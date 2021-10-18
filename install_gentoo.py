import argparse

from dist.gentoo import GentooInstaller


def parse_args(parser):

    args = parser.parse_args()

    cfg = args.config_file
    kfg = args.kconfig
    log = args.logpath
    mp = args.mount_point
    if not log:
        log = 'sup.log'

    if not mp:
        mp = '/mnt/gentoo'

    inst = GentooInstaller(cfg, mount_point=mp, kconfig=kfg, logpath=log, no_chroot=args.no_chroot)

    tasks = [args.prepdisks, args.getstage, args.setupenv, args.sysconf, args.config_portage,
             args.update_world, args.kernel, args.fstab, args.packages, args.services,
             args.bootloader, args.network, args.users, args.misc_config, args.display, args.all]
    if not any(tasks):
        parser.print_help()
        return

    if args.all or args.prepdisks:
        inst.prepare_disks()
        inst.mount_rootfs()

    if args.all or args.getstage:
        inst.get_stage()

    if args.all or args.setupenv:
        inst.setup_environment()

    if args.all or args.config_portage:
        inst.config_portage()

    if args.all or args.sysconf:
        inst.config_system_info()

    if args.all or args.update_world:
        inst.update_world_set()

    if args.all or args.kernel:
        inst.build_kernel()

    if args.all or args.fstab:
        inst.format_fstab()

    if args.all or args.packages:
        inst.get_misc_packages()

    if args.all or args.network:
        inst.config_network()

    if args.all or args.services:
        inst.install_services()

    if args.all or args.bootloader:
        inst.config_boot_loader()

    if args.all or args.misc_config:
        inst.do_misc_config()

    if args.all or args.display:
        inst.config_display()

    if args.all or args.users:
        inst.setup_users()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Install Gentoo from a configuration file')
    parser.add_argument('-f', '--file', action='store', dest='config_file', required=True,
                        help='Path to config file used for install')
    parser.add_argument('-l', '--logpath', action='store', dest='logpath', required=False,
                        help='Path to the installation log file')
    parser.add_argument('-a', '--all', action='store_true', dest='all', help='Performs a full gentoo install')
    parser.add_argument('-c', '--no-chroot', action='store_true', dest='no_chroot',
                        help='Performs install actions without using chroot (not recommended)')
    parser.add_argument('-k', '--kconfig', action='store', dest='kconfig', required=False,
                        help='Optional path to kconfig file used for the kernel build')
    parser.add_argument('-m', '--mount', action='store', dest='mount_point', required=False,
                        help='Specifies the mount point for the install (default is /mnt/gentoo)')

    parser.add_argument('--prepdisks', action='store_true', help='Formats disks, partitions, and LVMs')
    parser.add_argument('--getstage', action='store_true', help='Downloads the install stage from gentoo.org')
    parser.add_argument('--setupenv', action='store_true', help='Sets up initial mounted environment')
    parser.add_argument('--config-portage', action='store_true',
                        help='Configures portage global and package specific variables')
    parser.add_argument('--sysconf', action='store_true', help='Sets up the base system configuration')
    parser.add_argument('--update-world', action='store_true', help='Recompiles the portage world set')

    parser.add_argument('--kernel', action='store_true',
                        help='Compiles the linux kernel along with modules and initrd if needed')
    parser.add_argument('--fstab', action='store_true', help='Modifies the Fstab file for the supplied disk config')
    parser.add_argument('--packages', action='store_true', help='Installs optional packages')

    parser.add_argument('--network', action='store_true', help='Configures the network interfaces')
    parser.add_argument('--services', action='store_true', help='Registers optional services')
    parser.add_argument('--bootloader', action='store_true', help='Installs and configures the bootloader')
    parser.add_argument('--misc_config', action='store_true', help='Sets options in misc config files')
    parser.add_argument('--users', action='store_true', help='Creates additional users')
    parser.add_argument('--display', action='store_true', help='Configures display manager and server')

    parse_args(parser)
