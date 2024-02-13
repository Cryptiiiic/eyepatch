from functools import cached_property
from struct import unpack

from capstone.arm64_const import ARM64_REG_X1

from eyepatch import AArch64Patcher, errors
from eyepatch.aarch64 import Insn
from eyepatch.iboot import types
from eyepatch.iboot.errors import InvalidPlatform, InvalidStage


class iBoot64Patcher(AArch64Patcher):
    def __init__(self, data: bytes):
        super().__init__(data)

    @cached_property
    def base(self) -> int:
        ldr = self.search_insn('ldr')
        addr = ldr.offset + ldr.info.operands[-1].imm
        return unpack('<Q', self.data[addr : addr + 8])[0]

    @cached_property
    def build_style(self) -> str:
        # While the build-style string exists in stage 1, it isn't referenced by anything else.
        if self.stage != types.iBootStage.STAGE_2:
            raise InvalidStage('build-style only available on stage 2 iBoot')

        bs_str = self.search_string('build-style')
        bs_xref = self.search_xref(bs_str.offset)

        es_func = self.search_insn('bl', bs_xref.offset)
        for insn in self.disasm(es_func.offset, reverse=True):
            if len(insn.info.operands) == 0:
                continue

            if insn.info.operands[0].reg != ARM64_REG_X1:
                continue

            offset = insn.offset + insn.info.operands[-1].imm
            if insn.info.mnemonic == 'ldr':
                offset = unpack('<Q', self.data[offset : offset + 8])[0] - self.base

            elif insn.info.mnemonic != 'adr':
                raise errors.SearchError('Failed to find build style')

            return self.search_string(offset=offset).string

    @cached_property
    def platform(self) -> int:
        plat_str = self.search_string('platform-name')
        xref = self.search_xref(plat_str.offset)
        adr = self.search_insn('adr', xref.offset, skip=1)

        chip_id = self.search_string(offset=adr.offset + adr.info.operands[-1].imm)

        if chip_id.string.startswith('s5l'):
            return int(chip_id.string[3:-1], 16)
        elif chip_id.string.startswith('t') or chip_id.string.startswith('s'):
            return int(chip_id.string[1:], 16)

        raise InvalidPlatform(f'Unknown platform: "{chip_id.string}"')

    @cached_property
    def stage(self) -> types.iBootStage:
        for stage1 in ('iBootStage1', 'iBSS', 'LLB'):
            try:
                self.search_string(f'{stage1} for ')
                return types.iBootStage.STAGE_1
            except errors.SearchError:
                pass

        for stage2 in ('iBootStage2', 'iBEC', 'iBoot'):
            try:
                self.search_string(f'{stage2} for ')
                return types.iBootStage.STAGE_2
            except errors.SearchError:
                pass

    @cached_property
    def version(self) -> types.iBootVersion:
        version_str = self.search_string('iBoot-')
        major, minor, patch = version_str.string[6:].split('.', maxsplit=2)
        return types.iBootVersion(int(major), int(minor), int(patch))

    @cached_property
    def ret0_gadget(self) -> Insn:
        try:
            insn = self.search_insns('mov w0, #0', 'ret')
        except errors.SearchError:
            # Failed to find "mov w0, #0" and "ret" instructions
            # Insert our own instructions into empty space
            asm_len = 2 * 0x4
            offset = self.data.find(b'\0' * asm_len)
            while offset != -1:
                if offset % 4 == 0:
                    break

                offset = self.data.find(b'\0' * asm_len, offset + 1)

            else:
                raise ValueError('No area big enough to place instructions')

            asm = self.asm('mov w0, #0; ret')
            self._data[offset : offset + asm_len] = asm

            insn = next(self.disasm(offset))

        return insn

    def patch_freshnonce(self) -> None:
        if self.stage != types.iBootStage.STAGE_2:
            raise InvalidStage('freshnonce patch only available on stage 2 iBoot')

        # Find "platform_get_usb_more_other_string" function
        nonc_str = self.search_string(' NONC:', exact=True)
        nonc_xref = self.search_xref(nonc_str.offset)

        # Find "platform_get_nonce" function
        cbz = self.search_insn('cbz', nonc_xref.offset, reverse=True)
        pgn_func = self.search_insn('bl', cbz.offset).follow_call()

        # Ensure "platform_consume_nonce" always gets called
        insn = self.search_insn('tbnz', pgn_func.offset)
        insn.patch('nop')

    def patch_security_allow_modes(self) -> None:
        # Find "security_allow_modes" function
        dbg_str = self.search_string('debug-enabled')
        dbg_xref = self.search_xref(dbg_str.offset)
        sam_func = self.search_insn('bl', dbg_xref.offset, 1).follow_call()

        # Patch to always return 1
        bne = self.search_insn('b.ne', sam_func.offset)
        bne.patch(f'b #{bne.info.operands[-1].imm}')

        mov = self.search_insn('mov', bne.offset + bne.info.operands[-1].imm)
        mov.patch('mov x0, #0x1')

    def patch_inject_print(self, string: str, insn: Insn) -> None:
        # Find "printf" function
        pst_str = self.search_string('power supply type')
        pst_xref = self.search_xref(pst_str.offset)
        printf_func = self.search_insn('bl', pst_xref.offset).follow_call()

        # Find enough space to inject string + code
        string = string.encode() + b'\0'
        data_len = len(string) + (8 * 0x4)

        offset = self.data.find(b'\0' * data_len)
        while offset != -1:
            if offset % 4 == 0:
                break

            offset = self.data.find(b'\0' * data_len, offset + 1)

        else:
            raise ValueError('No area big enough to place data + string')

        # Function that saves x0 to stack, calls printf,
        # restores x0, calls original instruction, then branches back to next instruction
        insns = [
            'sub sp, sp, #0x4',
            'str x0, [sp, #0]',
            'adr x0, #0x18',
            f'bl #{hex(printf_func.offset - offset)}',
            'ldr x0, [sp, #0]',
            'add sp, sp, #0x4',
        ]

        # If original insn is relative address, change to be relative to our code
        if insn.info.mnemonic in ('b', 'bl', 'cbnz', 'cbz', 'adr', 'tbz', 'tbnz') or (
            insn.info.mnemonic == 'ldr' and len(insn.info.operands) == 2
        ):
            op_str = insn.info.op_str.replace(
                hex(insn.info.operands[-1].imm),
                hex(insn.info.operands[-1].imm + insn.offset - offset),
            )
        else:
            op_str = insn.info.op_str

        insns.append(f'{insn.info.mnemonic} {op_str}')
        insns.append(f'b #{hex(next(insn).offset - offset)}')

        asm = self.asm(';'.join(insns))

        self._data[offset : offset + data_len] = asm + string
        # Patch original instruction to branch to our function
        insn.patch(f'b #{hex(offset - insn.offset)}')

    def patch_nvram(self):
        if self.stage != types.iBootStage.STAGE_2:
            raise InvalidStage('NVRAM patch only available on stage 2 iBoot')

        # Find "env_blacklist_nvram" function
        dbg_str = self.search_string('debug-uarts')
        wl_offset = self.data.rfind(
            (dbg_str.offset + self.base).to_bytes(0x8, 'little')
        )
        while True:
            data = self.data[wl_offset : wl_offset + 0x8]
            if unpack('<Q', data)[0] == 0x0:
                wl_offset += 0x8
                break

            wl_offset -= 0x8

        ebn_func = self.search_xref(wl_offset).function_begin()

        # Patch to set return value as 0
        ebn_beq = self.search_insn('b.eq', ebn_func.offset)
        ebn_beq.patch(f'b #{hex(ebn_beq.info.operands[-1].imm)}')
        ebn_beq_call = ebn_beq.follow_call()
        ebn_beq_call.patch('mov w0, #0')

        # Ensure no other code changes the return value
        insn = next(ebn_beq_call)
        while insn.info.mnemonic != 'ldp':
            insn.patch('nop')
            insn = next(insn)

        if self.build_style == 'RELEASE':
            # Find "env_blacklist" function
            # Only exists on RELEASE builds
            eb_func = self.search_insn(
                'adr', ebn_func.offset, reverse=True
            ).function_begin()

            # Patch to always return 0
            eb_beq = self.search_insn('b.eq', eb_func.offset)
            eb_beq.patch(f'b #{hex(eb_beq.info.operands[-1].imm)}')
            eb_beq_call = eb_beq.follow_call()
            eb_beq_call.patch('mov w0, #0')

            # Ensure no other code changes the return value
            insn = next(eb_beq_call)
            while insn.info.mnemonic != 'ldp':
                insn.patch('nop')
                insn = next(insn)

        # Find "hide_key" function
        cas_str = self.search_string('com.apple.System.', exact=True)
        hk_func = self.search_xref(cas_str.offset).function_begin()
        bl = self.search_insn('bl', hk_func.offset)
        bl.patch('mov w0, #0x1')

    def patch_sigchecks(self):
        # Find "image4_validate_property_callback" function'
        ivpc_func = self.search_imm(int.from_bytes(b'BNCH', 'big')).function_begin()

        # Patch to always return 0
        ivpc_ret = self.search_insn('ret', ivpc_func.offset)

        ivpc_ret.patch(f'b #{hex(self.ret0_gadget.offset - ivpc_ret.offset)}')

    def patch_apfs_corruption(self):
        # Find "platform_get_drbg_personalization" function
        mov = self.search_imm(0x20000100)
        pgdp_func = mov.function_begin()

        # Find "platform_get_boot_manifest_hash" call
        target = self.search_insn('add', mov.offset, reverse=True)
        for insn in self.disasm(target.offset, reverse=True):
            if insn.offset < pgdp_func.offset:
                # TODO: Raise error
                pass

            if insn.info.mnemonic != 'add':
                continue

            if insn.info.operands[-1].imm == target.info.operands[-1].imm:
                pgbnh_call = self.search_insn('bl', insn.offset)
                break

        # Patch to always return 0
        pgbnh_call.patch('mov w0, #0')
