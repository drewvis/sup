import argparse

from dist.arch import ArchInstaller


def parse_args(parser):

    args = parser.parse_args()

    cfg = args.config_file
    kfg = args.kconfig
    log = args.logpath
    if not log:
        log = 'sup.log'

    inst = ArchInstaller(cfg, kconfig=kfg, logpath=log)

    tasks = [args.prepdisks, args.setupenv, args.kernel, args.fstab, args.packages, args.pacstrap,
             args.services, args.bootloader, args.network, args.users, args.dm, args.all]
    if not any(tasks):
        parser.print_help()
        return

    if args.all or args.prepdisks:
        inst.prepare_disks()

    if args.all or args.pacstrap:
        inst.pacstrap()

    if args.all or args.setupenv:
        inst.setup_environment()
        inst.config_system_info()

    if args.all or args.kernel:
        inst.build_kernel()

    if args.all or args.fstab:
        inst.format_fstab()

    if args.all or args.packages:
        inst.get_misc_packages()
        inst.remove_misc_packages()

    if args.all or args.services:
        inst.install_services()

    if args.all or args.dm:
        inst.config_display_manager()

    if args.all or args.bootloader:
        inst.config_boot_loader()

    if args.all or args.network:
        inst.config_network()

    if args.all or args.users:
        inst.setup_users()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Install Arch Linux from a configuration file')
    parser.add_argument('-f', '--file', action='store', dest='config_file', required=True,
                        help='Path to config file used for install')
    parser.add_argument('-l', '--logpath', action='store', dest='logpath', required=False,
                        help='Path to the installation log file')
    parser.add_argument('-a, --all', action='store_true', dest='all', help='Performs a full Arch install')
    parser.add_argument('-k', '--kconfig', action='store', dest='kconfig', required=False,
                        help='Optional path to kconfig file used for the kernel build')

    parser.add_argument('--prepdisks', action='store_true', help='Formats disks, partitions, and LVMs')
    parser.add_argument('--pacstrap', action='store_true', help='Configures and installs pacman')

    parser.add_argument('--setupenv', action='store_true', help='Sets up initial mounted environment')
    parser.add_argument('--kernel', action='store_true',
                        help='Compiles the linux kernel along with modules and initrd if needed')
    parser.add_argument('--fstab', action='store_true', help='Modifies the Fstab file for the supplied disk config')

    parser.add_argument('--packages', action='store_true', help='Installs optional packages')
    parser.add_argument('--services', action='store_true', help='Registers optional services')
    parser.add_argument('--dm', action='store_true', help='Configures an optional display manager')

    parser.add_argument('--bootloader', action='store_true', help='Installs and configures the bootloader')
    parser.add_argument('--network', action='store_true', help='Configures the network interfaces')
    parser.add_argument('--users', action='store_true', help='Creates additional users')

    parse_args(parser)
