/* File-backed firmware test fixture. Never included in user installation media. */
static void outb(unsigned short port, unsigned char value)
{
    __asm__ volatile ("outb %0, %1" : : "a"(value), "Nd"(port));
}

static unsigned char inb(unsigned short port)
{
    unsigned char value;
    __asm__ volatile ("inb %1, %0" : "=a"(value) : "Nd"(port));
    return value;
}

__attribute__((ms_abi)) unsigned long long efi_main(void *image, void *system)
{
    (void)image;
    (void)system;
    outb(0x3fb, 0x80);
    outb(0x3f8, 1);
    outb(0x3f9, 0);
    outb(0x3fb, 3);
    /* Immediate bytes keep this tiny cross-format fixture free of data relocations. */
#define SEND(c) do { while (!(inb(0x3fd) & 0x20)) {} outb(0x3f8, (c)); } while (0)
    SEND('R'); SEND('U'); SEND('F'); SEND('U'); SEND('S'); SEND('_');
    SEND('U'); SEND('E'); SEND('F'); SEND('I'); SEND('_'); SEND('B');
    SEND('O'); SEND('O'); SEND('T'); SEND('_'); SEND('O'); SEND('K');
    SEND('\r'); SEND('\n');
    outb(0xf4, 0x12);
    for (;;) { __asm__ volatile ("hlt"); }
}
