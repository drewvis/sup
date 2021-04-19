import os
import re
import grp
import pwd
import multiprocessing
from shutil import copyfile, rmtree
from urllib.parse import urlparse

from dist.linux import LinuxInstaller, InstallException, CmdError


class ArchInstaller(LinuxInstaller):

    '''Installs a base Arch Linux system from a supplied configuration file'''

    def __init__(self, config, mount_point='/mnt', logpath='sup.log', kconfig=None):
        super(ArchInstaller, self).__init__(config, logpath=logpath, kconfig=kconfig)
        self.mount_point = mount_point
        self.pacman = self.config.get('pacman')

    def config_mirrors(self):

        '''Configure the pacman mirrors'''

        mirror_file = '/etc/pacman.d/mirrorlist'

        self.logger.info('Configuring mirrors')

        mirrors = self.pacman.get('mirrors')
        if not mirrors:
            self.logger.info('No mirrors supplied, using defaults')
            return

        back_file = '%s.bak' % (mirror_file)
        if not os.path.exists(back_file):
            copyfile(mirror_file, back_file)

        with open(mirror_file, 'r') as f:
            lines = f.readlines()
            if len(lines) < 2:
                raise InstallException('Invalid pacman mirrorlist')

            cre = re.compile('(?<=\#\# )[a-zA-Z ]+')
            sre = re.compile('(?<=Server \= )\S+')
            out_lines = []
            for i, l in enumerate(lines):
                server = sre.search(l)
                if server:
                    if not out_lines:
                        out_lines += lines[: i - 1]

                    server = server.group(0)

                    sline = l
                    cline = lines[i - 1]
                    country = cre.search(cline)
                    if not country:
                        raise InstallException('Invalid pacman mirrorlist')
                    country = country.group(0)

                    host = urlparse(server)
                    host = host[1]

                    # See if the country is whitelisted
                    whitelist = mirrors.get('countries')
                    if whitelist:
                        whitelist = [c if c != 'usa' else 'united states' for c in [x.lower() for x in whitelist]]

                    if country.lower() in whitelist:
                        out_lines += [cline, sline]

                    whitelist = mirrors.get('servers')
                    if whitelist:
                        if [s for s in whitelist if s.lower() in host]:
                            out_lines += [cline, sline]

            out_lines += '\n'
            with open(mirror_file, 'w') as wf:
                wf.writelines(out_lines)

    def check_chroot(func):

        def wrap(self, *args):
            self.mount_block_devs()
            self.do_chroot()
            func(self, *args)
        return wrap

    @check_chroot
    def format_fstab(self, disks=[], lvms=[]):

        '''Format the fstab file'''

        if not disks:
            disks = self.disks
        if not lvms:
            lvms = self.lvms
        super(ArchInstaller, self).format_fstab(disks, lvms)

    @check_chroot
    def set_time_zone(self, timezone):

        '''Sets the timezone and informs portage'''

        self.logger.info('Setting time zone')

        if not timezone:
            return

        if not os.path.exists('/usr/share/zoneinfo/%s' % (timezone)):
            raise InstallException('Invalid timezone')

        with open('/etc/timezone', 'w+') as f:
            f.write(timezone)
            f.flush()

    @check_chroot
    def config_keyboard(self, keymap):
        self.logger.info('Setting keyboard settings')
        self.update_file_var('/etc/vconsole.conf', 'KEYMAP', keymap)

    @check_chroot
    def set_locales(self, locales):

        '''Sets the current locale'''

        self.logger.info('Setting locales')

        with open('/etc/locale.gen', 'r+') as f:
            lines = f.readlines()
            for i, l in enumerate(lines):
                if re.search('#\s+\w', l):
                    continue

                tmpl = re.sub('[\.\-# ]', '', l).lower()
                for loc in locales:
                    tmpc = re.sub('[\.\-# ]', '', loc).lower()
                    if tmpl.startswith(tmpc):
                        if l.startswith('#'):
                            l = l[1:] # noqa
                            self.logger.info('Setting locale: %s' % (l))
                            lines[i] = l
            f.seek(0)
            f.writelines(lines)
            f.truncate()

        r, o = self.exec_cmd(['locale-gen'])

    @check_chroot
    def config_clock(self, hwclock):
        self.logger.info('Setting system clock')

        timescales = {'local': '--localtime', 'UTC': '--utc'}
        hwc = timescales.get(hwclock)
        if hwc:
            self.exec_cmd(['hwclock', '--systohc', hwc])

    @check_chroot
    def config_system_info(self, sysinfo={}):

        '''Configure system information: keymap, clock, locales, timezone, and sudoers file'''

        self.exec_cmd(['systemd-machine-id-setup'])

        if not sysinfo:
            sysinfo = self.sysconfig

        keymap = sysinfo.get('keymap')
        clock = sysinfo.get('clock')
        locales = sysinfo.get('locales')
        timezone = sysinfo.get('timezone')
        sudo = sysinfo.get('sudo', [])

        self.set_locales(locales)
        self.config_keyboard(keymap)
        self.set_time_zone(timezone)
        self.config_clock(clock)

        # Update sudoers file if necessary
        sudoers = '/etc/sudoers'
        if sudo:
            if not os.path.exists(sudoers):
                self.packman_install('core/sudo', flags=['--noconfirm'])

            with open(sudoers, 'r+') as f:
                lines = f.readlines()
                for i in sudo:
                    if i not in lines:
                        lines.append(i.rstrip() + '\n')

                f.seek(0)
                f.writelines(lines)
                f.truncate()

    def setup_environment(self, pacman={}):

        '''Setup the arch environment'''

        # Copy the DNS info
        self.exec_cmd(['cp', '-L', '/etc/resolv.conf', self.mount_point + '/etc/'])

        # Mount the file systems

        if not self.is_mounted(self.mount_point + '/proc'):
            self.exec_cmd(['mount', '-t', 'proc', 'proc', self.mount_point + '/proc'])

        if not self.is_mounted(self.mount_point + '/sys'):
            self.exec_cmd(['mount', '--rbind', '--make-rslave', '/sys', self.mount_point + '/sys'])

        if not self.is_mounted(self.mount_point + '/dev'):
            self.exec_cmd(['mount', '--rbind', '--make-rslave', '/dev', self.mount_point + '/dev'])

    @check_chroot
    def config_network(self, network={}):

        '''Configure the network interfaces for systemd'''

        if not network:
            network = self.network

        self.logger.info('Configuring network')

        hostname = network.get('hostname')
        interfaces = network.get('interfaces', [])
        netmgr = network.get('manager', '').lower()

        # Update the hostname information
        with open('/etc/hostname', 'w') as f:
            f.write('%s\n' % (hostname))

        if netmgr == 'networkd':

            for i in interfaces:
                name = i['name']
                fpath = '/etc/systemd/network/%s.network' % (name)

                self.update_ini_file(fpath, {'Match': {'Name': name}})

                # Add any other systemd .network paramters now
                sysd = i.get('.network', {})
                self.update_ini_file(fpath, sysd)

            self.exec_cmd(['systemctl', 'enable', 'systemd-networkd.service'])

            # Config systemd to manage DNS
            self.exec_cmd(['ln', '-snf', '/run/systemd/resolve/resolv.conf', '/etc/resolv.conf'])
            self.exec_cmd(['systemctl', 'enable', 'systemd-resolved.service'])

        elif netmgr == 'networkmanager':
            self.packman_install('extra/networkmanager', flags=['--noconfirm'])
            self.exec_cmd(['systemctl', 'enable', 'NetworkManager'])

    @check_chroot
    def config_mkinitcpio(self, bootloader={}, use_systemd=False):

        cfg = '/etc/mkinitcpio.conf'

        # Update hooks
        hooks = self.get_file_var(cfg, 'HOOKS')
        hooks = hooks.strip('()').split()

        if self.is_root_an_lvm():
            self.packman_install('core/lvm2', flags=['--noconfirm'])
            val = 'lvm2'
            if use_systemd:
                val = 'sd-%s' % (val)

            if val not in hooks:
                # Need to add lvm2 immediately after the block hook
                i = hooks.index('block')
                hooks.insert(i + 1, val)

        if self.is_root_encrypted():
            val = 'encrypt'
            if use_systemd:
                val = 'sd-%s' % (val)

            if val not in hooks:
                i = hooks.index('block')
                hooks.insert(i + 1, val)

        # Do we want to support swap file hibernation?
        hiber_cfg = bootloader.get('hibernation')
        if hiber_cfg:
            val = 'resume'
            i = hooks.index('fsck')
            hooks.insert(i, val)

        hooks = ' '.join(hooks)
        hooks = '(%s)' % (hooks)
        self.update_file_var(cfg, 'HOOKS', hooks, use_quotes=False)

        # Rebuild the initramfs image
        self.exec_cmd(['mkinitcpio', '-p', 'linux'])

    def build_custom_kernel(self):
        def _get_dir_owner(path):
            stat_info = os.stat(path)
            uid = stat_info.st_uid
            gid = stat_info.st_gid

            user = pwd.getpwuid(uid)[0]
            group = grp.getgrgid(gid)[0]
            return user, group

        def _get_valid_pgp_keys(path):
            key_lines = []
            with open(path, 'r') as f:
                lines = f.readlines()
                grab_lines = False
                for i, l in enumerate(lines):
                    if 'validpgpkeys' in l and '(' in l:
                        grab_lines = True
                    elif grab_lines:
                        hit = re.search('\'[A-Za-z0-9]+\'', l)
                        if hit:
                            key_lines.append(hit.group(0).strip('\''))

                    if (l.find(')') >= 0 and # noqa
                       (l.find('#') < 0 or l.find(')') < l.find('#')) and grab_lines):
                        break
            return key_lines

        base_dir = '/usr/src/linux_build'
        build_dir = base_dir + '/linux/trunk'
        pkg_name = 'linux-custom'
        sudoers = '/etc/sudoers'
        nobody = 'nobody ALL=(ALL) NOPASSWD: ALL\n'
        delete_dirs = []
        perm_cleanups = []

        try:
            self.packman_install('extra/asp', flags=['--noconfirm'])
            if not os.path.isdir(base_dir):
                os.mkdir(base_dir)

            os.chdir(base_dir)

            self.exec_cmd(['asp', 'update', 'linux'])
            if os.path.isdir('linux'):
                rmtree('linux')
            self.exec_cmd(['asp', 'checkout', 'linux'])

            os.chdir(build_dir)

            # Copy the supplied kernel config if supplied
            if self.kconfig:
                with open('config', 'w') as f:
                    data = self.kconfig.read()
                    f.write(data)

                    self.kconfig.seek(os.SEEK_SET)

            # Install firmware if necessary
            with open('config', 'r') as f:
                lines = f.readlines()
                firmware = self.get_var_val('CONFIG_EXTRA_FIRMWARE', lines)
                if firmware and len(firmware):
                    self.packman_install('core/linux-firmware', flags=['--noconfirm'])

            with open('PKGBUILD', 'r+') as f:
                lines = f.readlines()

                pkgbase_idx = 0
                for i, line in enumerate(lines):
                    if 'pkgbase=' in line and not pkgbase_idx:
                        pkgbase_idx = i
                        break

                new = 'pkgbase=%s\n' % (pkg_name)
                if new not in lines:
                    lines.insert(pkgbase_idx + 1, new)

                f.seek(0)
                f.writelines(lines)
                f.truncate()

            # Get the valid PGPG keys
            keys = _get_valid_pgp_keys('PKGBUILD')

            self.packman_install('community/pacman-contrib', flags=['--noconfirm'])

            # Update sudoers file if necessary
            # makepkg forbids anyone, ever, from running it as root
            # Temporarily add nobody to sudoers file
            with open(sudoers, 'r+') as f:
                lines = f.readlines()
                if nobody not in lines:
                    lines.append(nobody)

                f.seek(0)
                f.writelines(lines)
                f.truncate()

            dir_owner, dir_group = _get_dir_owner(build_dir)
            self.exec_cmd(['chown', '-R', 'nobody', build_dir])
            self.exec_cmd(['sudo', '-u', 'nobody', 'updpkgsums'])
            perm_cleanups.append((build_dir, dir_owner, dir_group))

            if not os.path.isdir('/.gnupg'):
                os.mkdir('/.gnupg')
                delete_dirs.append('/.gnupg')
                self.exec_cmd(['chown', '-R', 'nobody', '/.gnupg'])

            for key in keys:
                self.exec_cmd(['sudo', '-u', 'nobody', 'gpg', '--recv-keys', key])

            self.packman_install('base-devel', flags=['--noconfirm'])

            cn = multiprocessing.cpu_count()
            try:
                r, o = self.exec_cmd('sudo -u nobody  MAKEFLAGS=\"-j%d\" makepkg -s --noconfirm' % (cn),
                                     shell=True)
            except CmdError as ce:
                if 'The package group has already been built' not in ce.output:
                    raise ce

            packages = [pkg for pkg in os.listdir('.')
                        if pkg.startswith(pkg_name) and pkg.endswith('pkg.tar.xz')]
            self.packman_install(packages, flags=['--noconfirm'], local=True)
        finally:
            for d in delete_dirs:
                rmtree(d)
            with open(sudoers, 'r+') as f:
                lines = f.readlines()
                if nobody in lines:
                    lines.remove(nobody)

                f.seek(0)
                f.writelines(lines)
                f.truncate()
            for path, owner, group in perm_cleanups:
                self.exec_cmd(['chown', '-R', '%s:%s' % (owner, group), path])

    @check_chroot
    def build_kernel(self, kernel={}):
        self.packman_install('linux', flags=['--noconfirm'])
        if self.kconfig:
            self.build_custom_kernel()
        self.config_mkinitcpio(self.bootloader)

    def pacstrap(self):

        '''Configures and installs pacman'''

        self.config_mirrors()
        self.mount_rootfs()
        self.mount_block_devs()
        self.exec_cmd(['pacstrap', self.mount_point, 'base'])

    @check_chroot
    def config_boot_loader(self, bootldr=''):

        '''Configure the system bootloader'''

        if not bootldr:
            bootldr = self.bootloader

        ldrname = bootldr.get('name')

        if not ldrname:
            raise InstallException('No boot loader specified')

        ldrname = ldrname.lower()

        # Get the block device used for boot
        parts = self.get_parts_by_attr('flags', val='boot')
        if len(parts) > 1:
            raise InstallException('More than one boot drive found')
        bootdev = parts[0][0]

        if ldrname not in ('grub',):
            raise InstallException('Invalid boot loader specified')

        if ldrname == 'grub':
            self._config_grub(bootdev, bootldr)
        else:
            raise InstallException('Only GRUB is supported for now')

    @check_chroot
    def _config_grub(self, bootdev, bootldr):

        '''Configures the grub bootloader'''

        fwiface = bootldr.get('fwiface')

        if not fwiface:
            raise InstallException('No firmware interface specified')
        if fwiface.lower() not in ('uefi', 'bios'):
            raise InstallException('Invalid firmware interface specified')

        self.exec_cmd(['pacman', '--noconfirm', '-S', 'core/grub'])

        parts = self.get_parts_by_attr('crypt')
        for disk, dev, part in parts:
            if disk == bootdev:
                self.update_file_var('/etc/default/grub', 'GRUB_ENABLE_CRYPTODISK', 'y')
                break

        if fwiface == 'uefi':
            if not self.which('efibootmgr'):
                self.packman_install('core/efibootmgr', flags=['--noconfirm'])
            r, o = self.exec_cmd(['grub-install', '--target=x86_64-efi', '--efi-directory=/boot'])
        else:
            r, o = self.exec_cmd(['grub-install', '--target=i386-pc', bootdev])

        # Set LVM cmdline if needed
        if len(self.lvms):
            self.merge_file_var('/etc/default/grub', 'GRUB_CMDLINE_LINUX', 'dolvm')

        self.merge_file_var('/etc/default/grub', 'GRUB_CMDLINE_LINUX', 'init=/usr/lib/systemd/systemd')

        # We need to tell the kernel if the root partition is on a crypt device
        dev = self.get_blk_dev_from_mount('/')
        if not dev:
            lv = self.get_lv_from_mount('/')
            if lv:
                pv, vg, vol, vname = lv
                pv = pv.get('physvol')
                bd = self.get_blk_dev_from_mapping(self.disks, pv)
                if bd:
                    # If root is on a crypt LVM mapping, tell the kernel where it is
                    uid = self.get_uuid_for_dev(bd)
                    self.merge_file_var('/etc/default/grub', 'GRUB_CMDLINE_LINUX',
                                        'cryptdevice=UUID=%s:cryptolvm' % (uid))

        # Add any additonal config values to grub
        conf = bootldr.get('conf')
        if conf:
            for c in conf:
                if len(c) != 2:
                    raise InstallException('Invalid GRUB config parameter')
                self.merge_file_var('/etc/default/grub', c[0], c[1])

        hiber_cfg = bootldr.get('hibernation')
        if hiber_cfg:
            swap_path = hiber_cfg.get('swap_path', '')
            # Is this a swap partition?
            if self.is_path_bock_dev(swap_path):
                self.merge_file_var('/etc/default/grub',
                                    'GRUB_CMDLINE_LINUX',
                                    'resume=%s' % (swap_path))
            else:
                NotImplementedError('Swapfile not supported for hibernation yet')

        self.packman_install('extra/intel-ucode', flags=['--noconfirm'])

        # LVM will wait in extremely long loops if it can't locate lvemetad
        # pass it through to the chroot now if we can
        self.exit_chroot()

        lvm_dir = '/run/lvm'
        if os.path.isdir(lvm_dir):
            lvm_dir = '/run/lvm'
            chroot_dir = '/tmplvm'
            host_dir = self.mount_point + chroot_dir

            if not os.path.isdir(host_dir):
                os.mkdir(host_dir)
            self.exec_cmd(['mount', '--bind', lvm_dir, host_dir])
            self.do_chroot()
            if os.path.isdir(lvm_dir):
                os.unlink(lvm_dir)
            self.exec_cmd(['ln', '-s', chroot_dir, lvm_dir])
            r, o = self.exec_cmd(['grub-mkconfig', '-o', '/boot/grub/grub.cfg'])
            os.unlink(lvm_dir)
            self.exit_chroot()
            self.exec_cmd(['umount', host_dir])
            os.rmdir(host_dir)

        else:
            self.do_chroot()
            r, o = self.exec_cmd(['grub-mkconfig', '-o', '/boot/grub/grub.cfg'])

    def packman_install(self, package, flags, local=False):
        if local:
            inst_opt = ['-U']
        else:
            inst_opt = ['-S']

        pacman = ['pacman'] + flags + inst_opt
        if isinstance(package, list):
            pacman += package
        else:
            pacman.append(package)
        self.exec_cmd(pacman)

    def packman_remove(self, package, flags):
        pacman = ['pacman'] + flags + ['-R']
        if isinstance(package, list):
            pacman += package
        else:
            pacman.append(package)
        self.exec_cmd(pacman)

    @check_chroot
    def get_misc_packages(self, pkgs=[], flags=[], skip_on_fail=False):

        '''Install any optional packages'''

        if not pkgs:
            pkgs = self.packages

        for pkg in pkgs:
            try:
                self.packman_install(pkg, flags=flags + ['--noconfirm'])
            except CmdError as ce:
                if not skip_on_fail:
                    raise ce
                else:
                    self.logger.error('Error installing %s, error=%s, output=%s' % (pkg, ce.error, ce.output))
                    continue

    @check_chroot
    def remove_misc_packages(self, pkgs=[], flags=[], skip_on_fail=True):
        '''Install any optional packages'''

        if not pkgs:
            pkgs = self.remove_packages

        for pkg in pkgs:
            try:
                self.packman_remove(pkg, flags=flags + ['--noconfirm'])
            except CmdError as ce:
                if not skip_on_fail:
                    raise ce
                else:
                    self.logger.error('Error uninstalling %s, error=%s, output=%s' % (pkg, ce.error, ce.output))
                    continue

    @check_chroot
    def install_services(self, svcs=[]):

        '''Install any optional services'''

        if not svcs:
            svcs = self.services

        for svc in svcs:
            self.exec_cmd(['systemctl', 'enable', svc])

    @check_chroot
    def config_display_manager(self, dm={}):

        '''Configure the display manager (if any)'''

        dm = dm or self.display
        dm = dm.get('manager')

        if not dm:
            return

        self.exec_cmd(['systemctl', 'enable', dm])
