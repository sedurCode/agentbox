# Used by the fault-snapshot host tool. Lives in ~/.config/agentbox/gdb (not the workspace).
set pagination off
set confirm off
set print pretty on
target extended-remote localhost:3333
monitor halt
echo \n=== core registers ===\n
info registers
echo \n=== backtrace ===\n
bt 16
echo \n=== stack (32 words at $sp) ===\n
x/32wx $sp
echo \n=== SCB fault status ===\n
printf "CFSR  0x%08x\n", *(unsigned int*)0xE000ED28
printf "HFSR  0x%08x\n", *(unsigned int*)0xE000ED2C
printf "DFSR  0x%08x\n", *(unsigned int*)0xE000ED30
printf "MMFAR 0x%08x\n", *(unsigned int*)0xE000ED34
printf "BFAR  0x%08x\n", *(unsigned int*)0xE000ED38
printf "AFSR  0x%08x\n", *(unsigned int*)0xE000ED3C
echo \n=== exception frame (if in handler, MSP/PSP stacked r0-r3,r12,lr,pc,xpsr) ===\n
x/8wx $msp
x/8wx $psp
detach
