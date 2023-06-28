import os
import re
import ssl
import time
import errno
import shutil
import hashlib
import tempfile
import binascii
import urllib.request
import multiprocessing

from dist.linux import LinuxInstaller, InstallException, CmdError


class GentooInstaller(LinuxInstaller):

    '''Installs a base Gentoo Linux system from a supplied configuration file'''

    def __init__(self, config, mount_point='/mnt/gentoo', logpath='sup.log', kconfig=None, no_chroot=False):
        super(GentooInstaller, self).__init__(config, kconfig=kconfig, logpath=logpath)
        self.mount_point = mount_point
        self.no_chroot = no_chroot
        self.profile_set = False

        self.portage = self.config.get('portage')
        self.stage = self.config.get('stage')
        self.packages_to_cleanup = []

        # Automated Weekly Release Key (https://www.gentoo.org/downloads/signatures/)
        self.eng_key_id = '13EBBDBEDE7A12775DFDB1BABB572E0E2D182910'
        self.key_server = 'hkps://keys.gentoo.org'

    def do_chroot(self):
        if self.no_chroot:
            return
        super(GentooInstaller, self).do_chroot()
        self.set_path()

    def exit_chroot(self):
        if not self.no_chroot:
            super(GentooInstaller, self).exit_chroot()

    def check_chroot(func):

        def wrap(self, *args):
            self.do_chroot()

            func(self, *args)

        return wrap

    def set_path(self):
        # Set the PATH inside of the chroot
        res = '(?<=[^A-Z]PATH=)\S+'
        res = re.compile(res)
        with open('/etc/profile.env') as f:
            data = f.read()
            hit = res.search(data)
            if hit:
                hit = hit.group(0)
                os.environ["PATH"] = hit
            else:
                raise InstallException('Failed to set chroot PATH')

    def set_package_value(self, path, name, *vals):

        '''Update a value for a package specific config'''

        nv = ' '.join(vals)

        if not os.path.isdir(path):
            # If it's a file, add to it
            with open(path, 'r') as f:
                lines = f.readlines()
                lines.append(nv)
                f.seek(0)
                f.writelines(lines)
                f.truncate()

        # Its a directory, make a new file
        else:
            path = '%s/%s' % (path, name)
            with open(path, 'w') as f:
                f.write(nv + '\n')
                f.truncate()

    def set_package_use(self, name, package, flags):

        '''Update local USE flags for a package'''

        path = '/etc/portage/package.use'
        if not os.path.exists(path):
            os.mkdir(path)

        self.set_package_value(path, name, package, flags)

    def set_package_mask(self, name, package):

        '''Update Masks for a package'''

        path = '/etc/portage/package.mask'
        if not os.path.exists(path):
            os.mkdir(path)

        self.set_package_value(path, name, package)

    def set_package_license(self, name, package, flags):

        '''Update Licenses for a package'''

        path = '/etc/portage/package.license'
        if not os.path.exists(path):
            os.mkdir(path)

        self.set_package_value(path, name, package, flags)

    def set_package_accept(self, name, package, keywords):

        '''Update keywords for a package'''

        path = '/etc/portage/package.accept_keywords'
        if not os.path.exists(path):
            os.mkdir(path)

        self.set_package_value(path, name, package, keywords)

    def _get_stage_url(self, stage):
        stagepath = stage.split('/')
        stagepath = '/'.join(stagepath[:-1])

        baseurl = 'https://distfiles.gentoo.org/releases/%s/' % (stagepath)

        stagepath = stage.split('/')
        stagepath = ''.join(stagepath[-1:])

        # The older stage path format
        res1 = '(?<=href=\")%s-\d{8}\.tar\.bz2' % (stagepath)
        res1 = re.compile(bytes(res1, 'utf-8'))

        # The newer (2017) stage path format
        res2 = '(?<=href=\")%s-\d{8}T\d{6}Z\.tar\.xz' % (stagepath)
        res2 = re.compile(bytes(res2, 'utf-8'))

        for res in (res1, res2):
            with urllib.request.urlopen(baseurl, context=ssl._create_unverified_context()) as f:
                data = f.read()
                hit = res.search(data)
                if hit:
                    hit = hit.group(0)
                    target = hit.decode('utf-8')
                    break

        if not hit:
            raise InstallException('Failed to get stage url')

        path = '%s%s' % (baseurl, target)
        return path

    # Can't be chroot'd here since it will break DNS
    def get_stage(self, stage=''):

        '''Downloads and verifies an install stage specified in the config file from gentoo.org'''

        retry_count = 100
        got_stage = False
        got_digests = False
        verified_sig = False

        if not stage:
            stage = self.stage

        self.logger.info('Downloading stage...')

        # Depending on mirrors, etc. we can sometimes not find a suitable stage url,
        # retry multiple times before we give up
        for i in range(retry_count):
            try:
                stage_url = self._get_stage_url(stage)
                self.logger.info('Getting stage from: %s' % (stage_url))
                # Get the tarball
                with urllib.request.urlopen('%s' % stage_url, context=ssl._create_unverified_context()) as t:
                    tar = b''
                    tsize = t.length
                    while t.length:
                        # TODO remove this magic val
                        tar += t.read(0x10000)
                        print("\r[*] Downloading Stage... [%.01f%%]" %
                              (100 * float(tsize - t.length) / float(tsize)), end='')
                    print('\n')
                    self.logger.info('Stage downloaded')

                    sha = hashlib.sha512()
                    sha.update(tar)
                    sha = sha.digest()
                    tarsha = binascii.hexlify(sha)
                    got_stage = True
            except urllib.error.HTTPError:
                time.sleep(1)
                self.logger.info('Got HTTP 404... retrying...')
                continue
            except InstallException:
                self.logger.info('Failed to find stage url... retrying...')
                continue
            break

        if not got_stage:
            raise InstallException('Failed to download stage')

        # Get the DIGESTS
        for i in range(retry_count):
            try:
                digests = '%s.DIGESTS' % (stage_url)
                self.logger.info('Getting stage digests from %s' % (digests))
                with urllib.request.urlopen(digests, context=ssl._create_unverified_context()) as d:
                    h = re.compile(b'(?<=SHA512 HASH\n)[a-fA-F0-9]+')
                    hashes = d.read()
                    hit = h.search(hashes).group(0)
                    if tarsha != hit:
                        raise InstallException('Stage hash mismatch')
                    got_digests = True
            except urllib.error.HTTPError:
                time.sleep(1)
                self.logger.info('Got HTTP 404... retrying...')
                continue
            break

        if not got_digests:
            raise InstallException('Failed to download digests')

        # Verify the digital signature
        for i in range(retry_count):
            try:
                with tempfile.NamedTemporaryFile() as tmp:
                    digests = '%s.DIGESTS' % (stage_url)
                    self.logger.info('Getting stage signature from %s' % (digests))
                    self.logger.info('Verifying stage signatures...')
                    with urllib.request.urlopen(digests, context=ssl._create_unverified_context()) as asc:
                        tmp.write(asc.read())
                        tmp.flush()
                        self.exec_cmd(['gpg', '--keyserver', self.key_server, '--recv-keys', self.eng_key_id])
                        rv, output = self.exec_cmd(['gpg', '--verify', tmp.name])
                        if 'Good signature' not in output:
                            raise InstallException('Signature check failed')
                        verified_sig = True
            except urllib.error.HTTPError:
                time.sleep(1)
                self.logger.info('Got HTTP 404... retrying...')
                continue
            break

        if not verified_sig:
            raise InstallException('Failed to verify stage signature')

        self.mount_rootfs()

        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(tar)
            tmp.flush()
            self.exec_cmd(['tar', 'xpf', tmp.name, '--xattrs', '-C', self.mount_point])

    def setup_environment(self, init='', portage={}):

        '''Sets up the initial install environment including mounts needed before chroot'''

        if not init:
            init = self.init
        if not portage:
            portage = self.portage

        self.logger.info('Setting up environment')

        try:
            os.mkdir('%s/etc/portage/repos.conf' % (self.mount_point))
        except FileExistsError:
            pass

        # Mount the file systems
        if not self.is_mounted(self.mount_point + '/proc'):
            self.exec_cmd(['mount', '-t', 'proc', 'proc', self.mount_point + '/proc'])

        if not self.is_mounted(self.mount_point + '/sys'):
            self.exec_cmd(['mount', '--rbind', '/sys', self.mount_point + '/sys'])

        if not self.is_mounted(self.mount_point + '/dev'):
            self.exec_cmd(['mount', '--rbind', '/dev', self.mount_point + '/dev'])

        # Mount /run
        r, o = self.exec_cmd(['mount', '--bind', '/run', self.mount_point + '/run'])
        r, o = self.exec_cmd(['mount', '--make-slave', self.mount_point + '/run'])

        if init == 'systemd':
            self.exec_cmd(['mount', '--make-rslave', self.mount_point + '/sys'])
            self.exec_cmd(['mount', '--make-rslave', self.mount_point + '/dev'])

        # Copy the repo config file
        repo_conf = '%s/etc/portage/repos.conf/gentoo.conf' % (self.mount_point)
        if not os.path.exists(repo_conf):
            shutil.copyfile('%s/usr/share/portage/config/repos.conf' % (self.mount_point),
                            repo_conf)

        # Copy the DNS info
        self.exec_cmd(['cp', '-f', '-L', '/etc/resolv.conf', self.mount_point + '/etc/'])

    @check_chroot
    def update_world_set(self):
        '''Update the portage @world set'''
        self.resync()

        # TODO FIXME: At the time of this comment, there has been a circular dependency between
        # hardfbuzz and freetype resulting in a failed rebuild of the world set. Explicitly resolve
        # this until the issue is resolved upstream.
        self.emerge_package('media-libs/freetype', flags=['--quiet-build', '--oneshot'],
                    env={**os.environ, **{'USE': '-harfbuzz'}})
        
        self.emerge_package(name='@world', flags=['--update', '--deep', '--newuse', '--quiet', '--quiet-build'])

    def trust_key(self, keyid, trustlevel):

        # List the fingerprint for the provided key
        r, o = self.exec_cmd(['gpg', '--homedir', '/var/lib/gentoo/gkeys/keyrings/gentoo/release',
                             '--fingerprint', keyid])

        fp = '[A-Z0-9]{4}\s+' * 10
        fp = re.compile(fp)
        hit = fp.search(o)
        if not hit:
            raise InstallException('Failed to get key fingerprint')
        fp = ''.join(hit.group(0).split())
        trust = '%s:%d:' % (fp, trustlevel)
        r, o = self.exec_cmd('echo %s | gpg --homedir /var/lib/gentoo/gkeys/keyrings/gentoo/release --import-ownertrust'
                             % (trust),
                             shell=True)

    def get_mirror_list(self, mirror_conf, rsync_mode=False):
        mirrorselect_cmd = ['mirrorselect', '-q', '-o']
        if rsync_mode:
            mirrorselect_cmd.append('-r')

        if not self.which('mirrorselect'):
            self.do_chroot()
            if not self.which('mirrorselect'):
                # emerge mirrorselect
                try:
                    self.exec_cmd(['emerge', '--quiet-build', '--oneshot', 'mirrorselect'])
                except CmdError as err:
                    if err.code != errno.EPERM:
                        raise err
                    self.resync()
                    self.exec_cmd(['emerge', '--quiet-build', '--oneshot', 'mirrorselect'])

        # Set the package mirrors
        country = mirror_conf.get('country')
        urls = mirror_conf.get('urls')
        if country:
            mirrorselect_cmd += ['-c', country]
        else:
            mirrorselect_cmd += ['-c', 'USA']
        r, o = self.exec_cmd(mirrorselect_cmd)

        if rsync_mode:
            output = o.split()
            if output[0] != 'sync-uri':
                raise InstallException('Got invalid sync uri format')
            mirror_list = output[-1]

        else:

            if urls:
                o = self.merge_flags_with_list(o, urls)

            mirror_list = o.split('\"')[1]
        return mirror_list

    def set_rsync_mirror(self, mirrors={}):

        mount_point = ''
        if not self.chrooted:
            mount_point = self.mount_point
        # Set the keyserver before we use portage
        cfg = {'gentoo': {'sync-openpgp-keyserver': self.key_server}}
        self.update_ini_file('%s/etc/portage/repos.conf/gentoo.conf' % (mount_point), cfg)

        rsync_mirror = mirrors.get('rsync')
        if not rsync_mirror:
            rsync_mirror = self.get_mirror_list(mirrors, rsync_mode=True)
            rsync_mirror += '/gentoo-portage/'

        cfg = {'gentoo': {'location': '/usr/portage',
                          'sync-type': 'rsync',
                          'sync-uri': rsync_mirror,
                          'sync-webrsync-verify-signature': 'true',
                          'auto-sync': 'yes',
                          'sync-rsync-verify-jobs': '1',
                          'sync-rsync-verify-metamanifest': 'yes',
                          'sync-rsync-verify-max-age': '24',
                          'sync-openpgp-key-path': '/usr/share/openpgp-keys/gentoo-release.asc',
                          'sync-openpgp-keyserver': self.key_server,
                          'sync-openpgp-key-refresh-retry-count': '40',
                          'sync-openpgp-key-refresh-retry-overall-timeout': '1200',
                          'sync-openpgp-key-refresh-retry-delay-exp-base': '2',
                          'sync-openpgp-key-refresh-retry-delay-max': '60',
                          'sync-openpgp-key-refresh-retry-delay-mult': '4'}}

        mount_point = ''
        if not self.chrooted:
            mount_point = self.mount_point
        self.update_ini_file('%s/etc/portage/repos.conf/gentoo.conf' % (mount_point), cfg)

    def config_portage(self, portage={}):

        '''Configures portage variables and profile'''

        mirror_list = ''

        if not portage:
            portage = self.portage

        mirrors = portage.get('mirrors', {})
        self.set_rsync_mirror(mirrors)

        if mirrors:
           mirror_list = self.get_mirror_list(mirrors)

        # Enter the chroot
        self.do_chroot()

        # Set the mirror list
        self.merge_file_var('/etc/portage/make.conf', 'GENTOO_MIRRORS', mirror_list)

        # Set the core count (this will be overridden by the config MAKEOPTS if supplied)
        cores = multiprocessing.cpu_count()
        self.update_file_var('/etc/portage/make.conf', 'MAKEOPTS', '-j%d' % (cores + 1))

        # Get the portage config vars
        varz = portage.get('vars')
        for v in varz:
            if len(v) != 2:
                raise InstallException('Invalid portage variable')
            self.update_file_var('/etc/portage/make.conf', v[0], v[1])

        # Set the package specific USE flags
        packuse = portage.get('packuse')
        if packuse:
            [self.set_package_use(n, p, f) for n, p, f in packuse]

        # Set any package accepted keywords
        packaccept = portage.get('packaccept')
        if packaccept:
            [self.set_package_accept(n, p, kw) for n, p, kw in packaccept]

        # Mask any unwanted packages
        packmask = portage.get('packmask')
        if packmask:
            [self.set_package_mask(n, p) for n, p in packmask]

        # Mask any unwanted packages
        packlic = portage.get('packlicense')
        if packlic:
            [self.set_package_license(n, p, f) for n, p, f in packlic]

        # Set system cpu flags if not specified in the config
        if 'cpu_flags_x86' not in [v[0].lower() for v in varz]:
            self.emerge_package('app-portage/cpuid2cpuflags', flags=['--quiet-build', '--oneshot'])
            r, o = self.exec_cmd(['cpuid2cpuflags'])
            cpu_flags = o.split()
            if cpu_flags[0] != 'CPU_FLAGS_X86:':
                raise InstallException('cpuid2cpuflags output in unknown format')
            cpu_flags = ' '.join(cpu_flags[1:])
            self.update_file_var('/etc/portage/make.conf', 'CPU_FLAGS_X86', cpu_flags)

        self.resync()
        # Update portage itself
        self.exec_cmd(['emerge', '--oneshot', 'portage'])

        self.exec_cmd(['emerge', '--info'])

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

        self.exec_cmd(['emerge', '--config', 'sys-libs/timezone-data'])

    def set_locales(self, locales):

        '''Sets the current locale'''

        self.logger.info('Setting locales')
        local_gen = []
        with open('/usr/share/i18n/SUPPORTED', 'r+') as f:
            lines = f.readlines()
            for loc_lookup in locales:
                for i, l in enumerate(lines):
                    line = l.lower().replace('-', '').replace(' ', '').replace('.', '')
                    ll = loc_lookup.lower().replace('-', '').replace(' ', '').replace('.', '')
                    if line.startswith(ll):
                        local_gen.append(l)

        to_write = []
        with open('/etc/locale.gen', 'r+') as f:
            lines = f.readlines()
            for i, l in enumerate(lines):
                tmpl = re.sub('[\.\-# ]', '', l).lower()
                for loc in local_gen:
                    tmpc = re.sub('[\.\-# ]', '', loc).lower()
                    if tmpl.startswith(tmpc):
                        if l.startswith('#'):
                            lines[i] = l[1:]
                    else:
                        if loc not in lines and loc not in to_write:
                            to_write.append(loc)
            lines += to_write
            f.seek(0)
            f.writelines(lines)
            f.truncate()

        r, o = self.exec_cmd(['locale-gen'])
        r, o = self.exec_cmd(['eselect', 'locale', 'list'], quiet=True)

        # We need at least one UTF8 locale
        if not any('utf8' in loc.replace('-', '').lower() for loc in locales):
            raise InstallException('Need at least one utf-8 locale')

        for loc in locales:
            nloc = loc.replace('-', '').lower()
            if nloc not in o.lower():
                raise InstallException('Invalid locale: %s' % (loc))

            if 'utf8' in nloc:
                self.exec_cmd(['eselect', 'locale', 'set', loc])

        r, o = self.exec_cmd(['env-update'])
        r, o = self.exec_cmd(['source /etc/profile'], shell=True)

    def install_initramfs(self, initramfs):
        # Install genkernel
        self.emerge_package('sys-kernel/genkernel', flags=['--quiet-build', '--newuse'],
                            env={**os.environ, **{'USE': '-firmware'}})

        # Parse the options
        opts = ' '.join(['--%s' % (o) for o in initramfs.split(' ')])

        if '--zfs' in opts.split():
            self.emerge_package('sys-fs/zfs', flags=['--quiet-build'])

        cl = 'genkernel %s --install initramfs' % (opts)
        cl = cl.split()
        r, o = self.exec_cmd(cl)

    @check_chroot
    def build_kernel(self, kernel={}):

        '''Build the kernel, initramrd, and modules from a specified kconfig'''

        if not kernel:
            kernel = self.kernel

        self.logger.info('Building kernel')

        # Make sure the boot partition is mounted
        self.mount_block_devs()

        # Config and build the kernel
        # If a generic kernel is requested, build it now
        genkernel = kernel.get('genkernel')
        if genkernel:
            # Install genkernel
            self.exec_cmd(['emerge', '--newuse', '--quiet-build', 'sys-kernel/genkernel'])
            # Genkernel reads the fstab so set it now
            self.format_fstab()

            # Format the genkernel commandline
            opts = genkernel.get('opts', '')
            action = genkernel.get('action', '')
            opts = opts.split()
            for i, opt in enumerate(opts):
                if not opt.startswith('--'):
                    opts[i] = '--%s' % (opt)
            cmd = ['genkernel'] + opts + [action]
            self.exec_cmd(cmd)

        else:

            # Get the requested kernel sources
            srcs = kernel.get('sources', 'sys-kernel/gentoo-sources')
            r, o = self.exec_cmd(['emerge', '--quiet-build', srcs])

            try:
                os.chdir('/usr/src/linux')
            except FileNotFoundError:
                self.exec_cmd(['eselect', 'kernel', 'set', '1'])
                os.chdir('/usr/src/linux')

            if self.kconfig:
                with open('.config', 'w') as f:
                    f.write(self.kconfig.read())
                    self.kconfig.seek(os.SEEK_SET)

            # Install firmware if necessary
            with open('.config', 'r') as f:
                lines = f.readlines()
                firmware = self.get_var_val('CONFIG_EXTRA_FIRMWARE', lines)
                if firmware and len(firmware):
                    self.emerge_package('sys-kernel/linux-firmware', flags=['--quiet-build'])
                    self.emerge_package('sys-firmware/sof-firmware', flags=['--quiet-build'])
                    self.emerge_package('net-wireless/wireless-regdb', flags=['--quiet-build'])

            # Compile the kernel
            targets = kernel.get('targets', '')
            targets = targets.split()
            cores = multiprocessing.cpu_count()
            if targets:
                cmd = ['make'] + targets
                r, o = self.exec_cmd(cmd)
            r, o = self.exec_cmd(['make', '-j%d' % (cores + 1)])

            # Install the kernel
            r, o = self.exec_cmd(['make', 'install'])

            # Install modules if necessary
            with open('.config', 'r') as f:
                lines = f.readlines()
                mods = self.get_var_val('CONFIG_MODULES', lines)
                modules = kernel.get('modules')

                if not mods and modules:
                    raise InstallException('Kmod loading disabled but auto modules were set')

                if mods and mods == 'y':
                    r, o = self.exec_cmd(['make', 'modules_install'])

                    # Set modules to autoload (if any)
                    built_modules = []
                    with open('modules.order', 'r') as f:
                        built_modules = [os.path.splitext(os.path.basename(m))[0] for m in f.readlines()]

                    # Validate we built the modules we want to autoload
                    auto = modules.split()
                    for a in auto:
                        if a not in built_modules:
                            raise InstallException('Module %s was not built, but set to autoload' % (a))

                    if modules and self.init == 'openrc':
                        self.update_file_var('/etc/conf.d/modules', 'modules', ' '.join(auto))

                    elif modules and self.init == 'systemd':
                        with open('/etc/modules-load.d/autoload.conf', 'w') as f:
                            f.writelines(['%s\n' % (a) for a in auto])

            # Install a initramfs if needed
            initramfs = kernel.get('initramfs')
            if initramfs:
                self.install_initramfs(initramfs)

    @check_chroot
    def auto_unmask(self, output):
        lines = output.splitlines()
        start = None
        end = None

        res1 = '(?<=required by )[a-zA-Z0-9\-\./]+(?= \(argument\))'
        res1 = re.compile(res1)

        res2 = '(?<=\=)[a-zA-Z0-9\-\./]+(?=\-\d)'
        res2 = re.compile(res2)

        ename = None
        for i, l in enumerate(lines):
            l = l.strip() # noqa

            if not ename:
                ename = res1.search(l)
                if ename:
                    ename = ename.group(0)
                    start = i + 1
            if not l:
                end = i

        if not ename or not start or not end:
            raise InstallException('Failed to autounmask package')

        use_lines = lines[start:end]
        use_lines = ' '.join(use_lines)

        # Isolate the package name
        usepack = res2.search(use_lines)
        if not usepack:
            raise InstallException('Failed to autounmask package')
        usepack = usepack.group(0)
        usename = usepack.split('/')[-1]

        data = use_lines.split()
        packuse = data[0]
        flags = ' '.join(data[1:])

        self.set_package_use(usename, packuse, flags)

    @check_chroot
    def format_fstab(self, disks=[], lvms=[]):

        '''Format the fstab file'''

        if not disks:
            disks = self.disks
        if not lvms:
            lvms = self.lvms
        super(GentooInstaller, self).format_fstab(disks, lvms)

    def normalize_package_name(self, package):

        '''Normalize a portage package name'''

        new = package
        fs = new.find('/')
        if fs > 0:
            new = new[fs + 1:]

        dash = new.find('-')
        if dash > 0 and new[dash + 1].isdigit():
            new = new[:dash]

        colon = new.find(':')
        if colon > 0:
            new = new[:colon]

        return new

    def emerge_package(self, name, flags=[], env={}):

        def _update_portage_file(dir_name, fname, lines):
            portage_base_dir = '/etc/portage/'

            target_dir = portage_base_dir + dir_name
            if not os.path.isdir(target_dir):
                os.mkdir(target_dir)
            lines = '\n'.join(lines) + '\n'
            with open('%s/%s' % (target_dir, fname), 'a') as f:
                f.write(lines)

        def _get_change_lines(input_lines, start_str):
            out_list = []
            for i, line in enumerate(input_lines):
                if start_str in line:
                    for sub_line in input_lines[i:]:
                        if not len(sub_line):
                            break
                        elif sub_line.startswith(('#', '<', '>', '=')):
                            out_list.append(sub_line)
            return out_list

        try:
            emerge = ['emerge'] + flags
            emerge.append(name)
            self.exec_cmd(emerge, env=env, truncate_errors=False)
        except CmdError as ce:

            if ce.code == errno.EPERM:
                if '--autounmask-write' in ce.output:

                    autounmask = self.portage.get('autounmask', 'interactive').lower()

                    if autounmask == 'interactive':
                        emerge = ['emerge', '--autounmask-write'] + flags
                        emerge.append(name)
                        try:
                            self.exec_cmd(emerge)
                        except CmdError:
                            self.logger.error('Error setting autounmask')

                        emerge = ['emerge', '--quiet-build'] + flags + [name]

                        # Open dispatch-conf to resolve the unmask conflict
                        self.logger.info('Resolving unmask conflict with dispatch-conf')
                        os.system('dispatch-conf')
                    elif autounmask == 'automerge':

                        lines = ce.output.splitlines()
                        pack_name = self.normalize_package_name(name)
                        self.logger.info('Automerging unmask conflict for %s' % (pack_name))

                        use_changes = _get_change_lines(lines, 'The following USE changes')
                        keyword_changes = _get_change_lines(lines, 'The following keyword changes')
                        license_changes = _get_change_lines(lines, 'The following license changes')

                        if use_changes:
                            _update_portage_file('package.use', pack_name, use_changes)
                        if keyword_changes:
                            _update_portage_file('package.accept_keywords', pack_name, keyword_changes)
                        if license_changes:
                            _update_portage_file('package.license', pack_name, license_changes)

                        emerge = ['emerge', '--quiet-build'] + flags
                        emerge.append(name)
                    self.exec_cmd(emerge)
                else:
                    raise ce

    @check_chroot
    def systemd_config_network(self, conf={}):

        '''Configure the network interfaces for systemd'''

        hostname = conf.get('hostname')
        interfaces = conf.get('interfaces', [])
        netmgr = conf.get('manager', '').lower()

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
            # self.exec_cmd(['ln', '-snf', '/run/systemd/resolve/resolv.conf', '/etc/resolv.conf'])
            # self.exec_cmd(['systemctl', 'enable', 'systemd-resolved.service'])

        elif netmgr == 'networkmanager':
            self.emerge_package('net-misc/networkmanager', flags=['--quiet-build'])
            self.exec_cmd(['systemctl', 'enable', 'NetworkManager'])

    @check_chroot
    def openrc_config_network(self, conf):

        '''Configure the network interfaces for openrc'''

        hostname = conf.get('hostname')
        domain = conf.get('domain')
        interfaces = conf.get('interfaces', [])
        netmgr = conf.get('manager', '').lower()

        # Update the hostname information
        if hostname:
            self.update_file_var('/etc/conf.d/hostname', 'hostname', hostname)

        # If netifrc (openrc) is being used configure it here
        if netmgr == 'netifrc':

            # Update the network configuration
            if domain:
                self.update_file_var('/etc/conf.d/net', 'dns_domain_lo', domain)

            for i in interfaces:
                self.update_file_var('/etc/conf.d/net', 'config_%s' % (i['name']), i['config'])

                routes = i['routes']
                if routes:
                    self.update_file_var('/etc/conf.d/net', 'routes_%s' % (i['name']), i['routes'])

            # Automatically start networking at boot
            for i in interfaces:
                try:
                    r, o = self.exec_cmd(['ln', '-s', '/etc/init.d/net.lo', '/etc/init.d/net.%s' % (i['name'])])
                except CmdError as err:
                    if err.code != errno.EPERM:
                        raise err
                self.exec_cmd(['rc-update', 'add', '/etc/init.d/net.%s' % (i['name']), 'default'])

        elif netmgr == 'networkmanager':
            self.emerge_package('net-misc/networkmanager', flags=['--quiet-build'])
            self.exec_cmd(['rc-update', 'add', 'NetworkManager', 'default'])

    def update_chroot_env(self):
        
        '''Config the new chroot for the gentoo environment'''

        if not os.path.exists('/etc/profile.env'):
            r, o = self.exec_cmd(['env-update'])
        
        with open('/etc/profile.env', 'r') as f:
            for l in f.readlines():
                if l.startswith('export'):
                    _, env = l.split(maxsplit=1)
                    var, val = env.split('=', maxsplit=1)
                    os.environ[var] = val
        locales = self.sysconfig.get('locales')
        if locales:
            os.environ['LC_ALL'] = locales[0]
            os.environ['LC_CTYPE'] = locales[0]

    @check_chroot
    def config_network(self, network={}):

        '''Configure network interfaces'''

        if not network:
            network = self.network

        self.logger.info('Configuring network')

        if self.init == 'openrc':
            self.openrc_config_network(network)
        elif self.init == 'systemd':
            self.systemd_config_network(network)

    def config_keyboard(self, keymap):
        self.logger.info('Setting keyboard settings')
        if self.init == 'openrc':
            self.update_file_var('/etc/conf.d/keymaps', 'keymap', keymap)
        elif self.init == 'systemd':
            self.update_file_var('/etc/vconsole.conf', 'KEYMAP', keymap)

    def config_clock(self, hwclock):
        self.logger.info('Setting system clock')

        if self.init == 'openrc':
            self.update_file_var('/etc/conf.d/hwclock', 'clock', hwclock)
        elif self.init == 'systemd':
            timescales = {'local': '--localtime', 'UTC': '--utc'}
            hwc = timescales.get(hwclock)
            if hwc:
                self.exec_cmd(['hwclock', '--systohc', hwc])

    @check_chroot
    def config_system_info(self, sysinfo={}):

        '''Configure system information: keymap, clock, locales, timezone, and sudoers file'''

        if not sysinfo:
            sysinfo = self.sysconfig

        keymap = sysinfo.get('keymap')
        clock = sysinfo.get('clock')
        locales = sysinfo.get('locales')
        timezone = sysinfo.get('timezone')
        sudo = sysinfo.get('sudo')

        # If systemd is used, create a machine ID for journaling
        if self.init == 'systemd':
            self.exec_cmd(['systemd-machine-id-setup'])

        self.set_locales(locales)
        self.config_keyboard(keymap)
        self.set_time_zone(timezone)
        self.config_clock(clock)

        # Update sudoers file if necessary
        sudoers = '/etc/sudoers'
        if sudo:
            if not os.path.exists(sudoers):
                self.emerge_package('app-admin/sudo', flags=['--quiet-build'])

        with open(sudoers, 'r+') as f:
            lines = f.readlines()
            for l in sudo: # noqa
                if l not in lines:
                    lines.append(l.rstrip() + '\n')

            f.seek(0)
            f.writelines(lines)
            f.truncate()

    @check_chroot
    def get_misc_packages(self, pkgs=[], flags=['--quiet-build'], skip_on_fail=False):

        '''Install any optional packages'''

        if not pkgs:
            pkgs = self.packages

        for pkg in pkgs:
            try:
                self.emerge_package(pkg, flags=flags)
            except CmdError as ce:
                if not skip_on_fail:
                    raise ce
                else:
                    self.logger.error('Error installing %s, error=%s, output=%s' % (pkg, ce.error, ce.output))
                    continue

    @check_chroot
    def install_services(self, svcs=[]):

        '''Install any optional services'''

        if not svcs:
            svcs = self.services

        for svc in svcs:
            if self.init == 'openrc':
                self.exec_cmd(['rc-update', 'add', svc, 'default'])
            else:
                self.exec_cmd(['systemctl', 'enable', svc])

    @check_chroot
    def _config_grub(self, bootdev, bootloader):

        '''Configures the grub bootloader'''

        fwiface = bootloader.get('fwiface')
        if len(self.lvms):
            with open('/etc/portage/package.use/grub2', 'w') as f:
                f.write('sys-boot/grub:2 device-mapper')

        if not fwiface:
            raise InstallException('No firmware interface specified')
        if fwiface.lower() not in ('uefi', 'bios'):
            raise InstallException('Invalid firmware interface specified')
        if fwiface == 'uefi':
            self.update_file_var('/etc/portage/make.conf', 'GRUB_PLATFORMS', 'efi-64')

        if not self.which('grub-mkconfig') and not self.which('grub-install'):
            self.exec_cmd(['emerge', '--verbose', '--quiet-build', 'sys-boot/grub:2'])

        parts = self.get_parts_by_attr('crypt')
        for disk, dev, part in parts:
            if disk == bootdev:
                self.update_file_var('/etc/default/grub', 'GRUB_ENABLE_CRYPTODISK', 'y')
                break

        if fwiface == 'uefi':
            r, o = self.exec_cmd(['grub-install', '--target=x86_64-efi', '--efi-directory=/boot'])
        else:
            r, o = self.exec_cmd(['grub-install', bootdev])

        # Set LVM cmdline if needed
        if len(self.lvms):
            self.merge_file_var('/etc/default/grub', 'GRUB_CMDLINE_LINUX', 'dolvm')

        # Tell the kernel of the systemd init (if we are using it)
        if self.init == 'systemd':
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
                    self.merge_file_var('/etc/default/grub', 'GRUB_CMDLINE_LINUX', 'crypt_root=UUID=%s' % (uid))

        # Add any additonal config values to grub
        conf = bootloader.get('conf')
        if conf:
            for c in conf:
                if len(c) != 2:
                    raise InstallException('Invalid GRUB config parameter')
                self.merge_file_var('/etc/default/grub', c[0], c[1])

        # os-probe will loop forever without a /run filesystem; mount it
        self.exit_chroot()
        r, o = self.exec_cmd(['mount', '--bind', '/run', self.mount_point + '/run'])
        r, o = self.exec_cmd(['mount', '--make-slave', self.mount_point + '/run'])
        self.do_chroot()

        hiber_cfg = bootloader.get('hibernation')
        if hiber_cfg:
            swap_path = hiber_cfg.get('swap_path', '')
            # Is this a swap partition?
            if self.is_path_bock_dev(swap_path):
                self.merge_file_var('/etc/default/grub',
                                    'GRUB_CMDLINE_LINUX',
                                    'resume=%s' % (swap_path))
            else:
                NotImplementedError('Swapfile not supported for hibernation yet')

            if os.path.exists('/etc/pm/config.d/gentoo'):
                self.update_file_var('/etc/pm/config.d/gentoo', 'SLEEP_MODULE', 'kernel')

        r, o = self.exec_cmd(['grub-mkconfig', '-o', '/boot/grub/grub.cfg'])

    @check_chroot
    def config_boot_loader(self, bootloader='', kernel=None):

        '''Configure the system bootloader'''

        if not bootloader:
            bootloader = self.bootloader

        ldrname = bootloader.get('name')

        if not ldrname:
            raise InstallException('No boot loader specified')

        ldrname = ldrname.lower()

        # If we need to support hibernation, we need access to the kernel
        # config as well for initramfs and kernel opts
        hiber_cfg = bootloader.get('hibernation')
        if bootloader.get('hibernation'):

            if not kernel:
                kernel = self.kernel
                if not kernel:
                    raise InstallException('Need kernel config to setup hibernation')

        # Make sure the boot partition is mounted
        self.mount_block_devs()

        # Get the block device used for boot
        parts = self.get_parts_by_attr('flags', val='boot')
        if len(parts) > 1:
            raise InstallException('More than one boot drive found')
        bootdev = parts[0][0]

        if ldrname not in ('grub',):
            raise InstallException('Invalid boot loader specified')

        if ldrname == 'grub':
            self._config_grub(bootdev, bootloader)
            # If we are setting up hibernation, reinstall initramfs
            if hiber_cfg:
                initramfs = kernel.get('initramfs')
                if initramfs:
                    self.install_initramfs(initramfs)
        else:
            raise InstallException('Only GRUB is supported for now')

    @check_chroot
    def config_display_manager(self, dm=''):

        '''Configure the display manager (if any)'''

        dm = dm or self.dm

        if not dm:
            return

        if self.init == 'openrc':
            # With display-manager
            if os.path.exists('/etc/conf.d/display-manager'):
                self.update_file_var('/etc/conf.d/display-manager', 'DISPLAYMANAGER', dm)
            # With the deprecated xdm init script
            if os.path.exists('/etc/conf.d/xdm'):
                self.update_file_var('/etc/conf.d/xdm', 'DISPLAYMANAGER', dm)
        elif self.init == 'systemd':
            self.exec_cmd(['systemctl', 'enable', dm])

        # Sometimes startDM.sh gets renamed to startDM.sh.1?
        if not os.path.isfile('/etc/X11/startDM.sh') and os.path.isfile('/etc/X11/startDM.sh.1'):
            self.logger.info('Resetting X startDM init script')
            shutil.copyfile('/etc/X11/startDM.sh.1', '/etc/X11/startDM.sh')
            self.exec_cmd(['chmod', '--reference=/etc/X11/startDM.sh.1', '/etc/X11/startDM.sh'])

    @check_chroot
    def config_display(self, display={}):
        display = display or self.display

        scripts = display.get('scripts', [])
        for script in scripts:

            fpath = script.get('file')
            if not fpath:
                raise InstallException('No script file path provided')

            if os.path.basename(fpath) not in ('xprofile', '.xprofile', 'xinitrc', '.xinitrc'):
                raise InstallException('Invalid display script file path')

            lines = script.get('lines', [])

            # If the script file doesnt exist, touch it
            if not os.path.exists(fpath):
                with open(fpath, 'a'):
                    os.utime(fpath, None)

            self.set_display_script(fpath, lines)

        # Configure a display manager
        mgr = display.get('manager')
        if mgr:
            self.config_display_manager(mgr)

        # Configure display servers
        servers = display.get('servers', [])
        for srv in servers:
            name = srv.get('name')
            if not name:
                raise InstallException('No display server type provided')

            # Configure X settings
            if name == 'xorg':
                for config in srv.get('configs', []):
                    fname = config.get('file')
                    if not fname:
                        raise InstallException('No display config file name given')
                    data = config.get('data')
                    if not data:
                        raise InstallException('No display config data given')

                    conf_dir = '/etc/X11/xorg.conf.d/'
                    path = '%s/%s' % (conf_dir, fname)
                    self.set_xorg_conf(path, data)
            elif name == 'wayland':
                raise NotImplementedError('TODO: support wayland config')
            else:
                raise InstallException('Unsupported display server: %s' % (name))

    @check_chroot
    def resync(self):
        def _set_profile():
            if not self.profile_set:
                # Select the portage profile
                profile = self.portage.get('profile')
                r, o = self.exec_cmd(['eselect', 'profile', 'list'])
                profset = False
                sre = re.compile('(?<=\s\s)[a-z0-9/\.]+')
                for line in o.split('\n'):
                    s = sre.search(line)
                    if s:
                        s = s.group(0).strip()
                        if s == profile:
                            profset = True
                            self.exec_cmd(['eselect', 'profile', 'set', s])
                if not profset:
                    raise InstallException('Unable to set portage profile')
                self.profile_set = True

        verify_retry_count = 40

        # Verify the Portage snapshot here
        for i in range(verify_retry_count):
            try:
                self.exec_cmd(['emerge', '--sync'])
                break
            except CmdError as ce:
                self.logger.info('Sync failed, retrying %d of %d' % (i, verify_retry_count))
                if ce.code != errno.EPERM:
                    raise ce
                _set_profile()
                if i >= verify_retry_count:
                    self.logger.error('Failed to verify Portage snapshot')
                    raise ce


