import os
import re
import time
import stat
import shlex
import errno
import fcntl
import shutil
import subprocess
import configparser

from collections import OrderedDict

from dist.unix import UnixInstaller, InstallException, CmdError

FILE_FMT_LVM = 0
FILE_FMT_VARVAL = 1
FILE_FMT_INI = 2


class LinuxInstaller(UnixInstaller):
    '''Generic Linux Installer'''

    def __init__(self, config, kconfig=None, logpath='sup.log'):

        super(LinuxInstaller, self).__init__(config, logpath=logpath)

        self.chrooted = False
        self.kconfig = None

        if kconfig:
            fn = kconfig
            if fn:
                if not os.path.exists(fn):
                    raise InstallException('Kernel config file not found: %s' % fn)
                self.kconfig = open(kconfig, 'r')

        self.arch = self.config.get('arch')
        self.disks = self.config.get('disks', [])
        self.lvms = self.config.get('lvms', [])
        self.sysconfig = self.config.get('sysconfig')
        self.kernel = self.config.get('kernel')
        self.packages = self.config.get('packages', [])
        self.remove_packages = self.config.get('remove_packages', [])
        self.bootloader = self.config.get('bootloader')
        self.misc_config = self.config.get('misc_config', [])
        self.users = self.config.get('users', [])
        self.display = self.config.get('display', {})
        self.services = self.config.get('services', [])

        self.init = self.sysconfig.get('init')

        # Get network params
        self.network = self.config.get('network')

    def check_chroot(func):

        '''Wrapper to make sure the func is running in chroot'''

        def wrap(self, *args):
            self.do_chroot()
            func(self, *args)
        return wrap

    def exit_chroot(self):
        if not self.realroot:
            raise InstallException('Tried to exit chroot when not in one')

        os.fchdir(self.realroot)
        os.chroot(".")
        self.logger.info('Exiting chroot...')
        self.chrooted = False

    def which(self, program):
        def is_exe(fpath):
            return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

        fpath, fname = os.path.split(program)
        if fpath:
            if is_exe(program):
                return program
        else:
            path_env = os.environ.get('PATH')
            if not path_env:
                return None
            for path in path_env.split(os.pathsep):
                path = path.strip('"')
                exe_file = os.path.join(path, program)
                if is_exe(exe_file):
                    return exe_file

        return None

    def exec_cmd(self, cmd, shell=False, quiet=False, truncate_errors=True, env=None):

        '''Execute a command/process'''

        output = b''

        lstr = ' '.join(cmd)
        if isinstance(cmd, str):
            lstr = cmd

        self.logger.info('exec: %s' % (lstr))

        if env:
            p = subprocess.Popen(cmd, shell=shell, env=env,
                                 stdin=None,
                                 stderr=subprocess.STDOUT,
                                 stdout=subprocess.PIPE)
        else:
            p = subprocess.Popen(cmd, shell=shell,
                                 stdin=None,
                                 stderr=subprocess.STDOUT,
                                 stdout=subprocess.PIPE)

        # Make the pipe non-blocking
        flags = fcntl.fcntl(p.stdout, fcntl.F_GETFL)
        flags = flags | os.O_NONBLOCK
        fcntl.fcntl(p.stdout, fcntl.F_SETFL, flags)

        while (p.poll() is None) or p.stdout.peek():
            try:
                o = p.stdout.read()
                if o and len(o):
                    if not quiet:
                        do = o.decode('utf-8', 'ignore').strip()
                        if len(do):
                            self.logger.info(do)
                    output += o
            except IOError:
                continue

        rcode = p.returncode
        if rcode != 0:
            self.logger.error(os.strerror(rcode))
            errmsg = output.decode('utf-8', 'ignore')
            # Truncate long error output
            if truncate_errors:
                if len(errmsg) > 2000:
                    errmsg = errmsg[-2000:]
            raise CmdError(os.strerror(rcode), rcode, errmsg)
        return rcode, output.decode('utf-8', 'ignore')

    def prepare_disks(self, disks=[], lvms=[]):

        '''Format the disks and LVMs if needed'''

        def _get_format_cmd(fs, dev, label=''):
            fs = fs.lower().strip()
            if fs.startswith('fat'):
                size = ''.join([c for c in fs if c.isdigit()])
                if not size:
                    raise InstallException('Invalid FS type: %s' % (fs))

                cmd = ['mkfs.fat', '-F', size]

            elif 'swap' == fs.lower():
                cmd = ['mkswap']
                if label:
                    cmd += ['-L', label]
            else:
                fs = fs.lower().strip()
                cmd = ['mkfs.%s' % (fs)]
                if label:
                    cmd += ['-L', label]
            return cmd + [dev]

        if not disks:
            disks = self.disks
        if not lvms:
            lvms = self.lvms

        self.logger.info('Preparing disks')
        if not disks:
            return

        for d in disks:

            dev_name = input('!!! WARNING: All data on %s will be erased; '
                             'enter the device name to confirm: ' % d['name'])
            if dev_name != d['name']:
                raise Exception('Failed to confirm disk overwrite, exiting')

            if d.get('label'):
                try:
                    self.exec_cmd(['parted', '-s', d['name'], 'mklabel', d['label']])
                except CmdError as err:
                    if err.code != errno.EPERM:
                        raise err

            for i, p in enumerate(d['partitions']):
                self.exec_cmd(['parted', '--align', 'optimal', '-s', d['name'],
                              'mkpart', p['type'], p['start'], p['end']])

                pidx = i + 1
                if d['name'].lower().startswith('/dev/nvme'):
                    part = '%sp%d' % (d['name'], pidx,)
                else:
                    part = '%s%d' % (d['name'], pidx,)
                pidx = str(pidx)

                n = p.get('name')
                if n:
                    self.exec_cmd(['parted', d['name'], 'name', pidx, n])

                flags = p.get('flags')
                if flags:
                    self.exec_cmd(['parted', d['name'], 'set', pidx, flags, 'on'])

                fs = p.get('fs')
                if fs:
                    cmd = _get_format_cmd(fs, part)
                    self.exec_cmd(cmd)

                c = p.get('crypt')
                if c:
                    self.exec_cmd(['modprobe', 'dm-crypt', 'aes', 'sha256'])

                    for i in range(10):
                        try:
                            self.exec_cmd(['cryptsetup', '--batch-mode', 'luksFormat', part])
                            break
                        except CmdError as err:
                            if err.code == errno.EINTR:
                                self.logger.info('cryptsetup partition not found, trying again...')
                                time.sleep(1)
                                continue
                            elif err.code != errno.EIO:
                                raise err
                            else:
                                break

                    if c.get('mapping'):
                        self.exec_cmd(['cryptsetup', 'open', '--type', 'luks',
                                       part, c['mapping']])

        for lvm in lvms:

            pv = lvm['physvol']
            # Looks like there can be a slight race here with partitions
            # not being found after creating physical partitions,
            # try multiple times with a 10 try timeout
            for i in range(10):
                try:
                    self.exec_cmd(['pvcreate', pv])
                    break
                except CmdError as err:
                    if err.code != errno.EIO:
                        raise err
                    else:
                        self.logger.info('pvcreate partition not found, trying again...')
                        time.sleep(1)
                        continue

            for g in lvm['volgroups']:
                gname = g['name']
                self.exec_cmd(['vgcreate', gname, pv])
                for vol in g['volumes']:
                    size = vol['size']
                    f = '-l' if '%' in size else '-L'
                    self.exec_cmd(['lvcreate', f, size, '-n', vol['name'], gname])

                    fs = vol.get('fs')
                    if fs:
                        cmd = _get_format_cmd(fs, '/dev/%s/%s' % (gname, vol['name']), label=vol.get('label'))
                        self.exec_cmd(cmd)

    def get_block_attrs(self):

        '''Get block device attributes used for the fstab'''

        r, o = self.exec_cmd(['blkid'], quiet=True)
        output = o.split('\n')
        pat = (r"(?P<dev>\S*(?=:))",
               r"(?P<uuid>(?<=UUID=\")\S*(?=\"))",
               r"(?P<type>(?<=TYPE=\")\S*(?=\"))",
               r"(?P<label>(?<=LABEL=\")\S*(?=\"))")

        blkids = []
        for _l in output:
            dt = {}
            [dt.update(_l.groupdict()) for _l in [re.search(p, _l) for p in pat] if _l]
            if len(dt):
                blkids.append(dt)
        return blkids

    def format_fstab(self, disks=[], lvms=[]):

        '''Update the fstab file'''

        def _get_fstab_line(blkids, mnt, opts, flags, dev):
            dump = 0
            npass = 0

            if opts:
                opts = opts.split()
                opts = ','.join([o for o in opts])
            else:
                opts = "defaults"

            if mnt:
                if flags and 'boot' in flags.split():
                    npass = 2
                if mnt == '/':
                    npass = 1
            else:
                mnt = 'none'

            for b in blkids:
                if b['dev'].lower() == dev.lower():
                    uuid = b.get('uuid')
                    # Use UUID instead of device name if possible
                    if uuid:
                        dev = 'UUID=%s' % (uuid)
                    return (dev, mnt, b.get('type'), opts, dump, npass)

        if not disks:
            disks = self.disks
        if not lvms:
            lvms = self.lvms

        blkids = self.get_block_attrs()

        fslines = []
        # Add disks to fstab
        for d in disks:
            i = 1
            for part in d['partitions']:
                mnt = part.get('mount')
                opts = part.get('opts')
                flags = part.get('flags')
                if not opts and not mnt:
                    continue
                dev = "%s%d" % (d['name'], i)

                fsl = _get_fstab_line(blkids, mnt, opts, flags, dev)
                if fsl and fsl[2]:
                    fslines.append(fsl)
                i += 1

        # Add the lvms to fstab
        for lv in lvms:
            for vg in lv['volgroups']:
                for v in vg['volumes']:
                    mnt = v.get('mount')
                    dev = '/dev/mapper/%s-%s' % (vg['name'], v['name'])
                    opts = v.get('opts')
                    flags = v.get('flags')

                    if not opts and not mnt:
                        continue

                    fsl = _get_fstab_line(blkids, mnt, opts, flags, dev)
                    fslines.append(fsl)

        # Get the existing lines from the current fstab saving only comment lines
        with open('/etc/fstab', 'r+') as fst:
            lines = fst.readlines()
            cmts = [_l for _l in lines if _l.lstrip().startswith('#')]
            fslines = ['%s\t\t%s\t\t%s\t\t%s\t\t%d %d\n' % _l for _l in fslines]

            lines = cmts + fslines

            fst.seek(0)
            fst.writelines(lines)
            fst.truncate()

    def is_path_bock_dev(self, path):
        try:
            return stat.S_ISBLK(os.stat(path).st_mode)
        except FileNotFoundError:
            return False

    def get_var_line(self, var, lines):

        '''Return the line number of a requested variable'''

        for i, l in enumerate(lines):
            vl = l.split('=')
            if len(vl) and vl[0].strip() == var:
                return i
        return None

    def get_var_val(self, var, lines):

        '''Return the value of a requested variable'''

        i = self.get_var_line(var, lines)
        if i:
            line = lines[i]
            idx = line.find('=')
            if idx != -1 and idx + 1 != len(line):
                val = line[idx + 1:]
                if len(val):
                    return val.strip('\" \t\r\n')
        return None

    def merge_flags_with_list(self, orig, new):

        '''Merge variables with a supplied list'''

        olist = orig.split("\"")
        flags = olist[1].split()

        flags += new
        flags = set(flags)

        olist[1] = ' '.join(flags)

        flags = '\"'.join(olist)
        return flags

    def merge_flags(self, orig, new):

        '''Merge two strings containing variables'''

        olist = orig.split("\"")
        ovname = olist[0]
        oflags = olist[1].split()

        nlist = new.split("\"")
        nvname = nlist[0]
        nflags = nlist[1].split()

        if ovname.strip() != nvname.strip():
            raise InstallException('Variable names don\'t match')

        flags = list(set(oflags + nflags))
        olist[1] = ' '.join(flags)
        flags = '\"'.join(olist)

        return flags

    def merge_file_var(self, fname, var, new):

        '''Merge variables with a file'''

        val = self.get_file_var(fname, var)
        if not val:
            self.update_file_var(fname, var, new)
            return

        ovals = val.split(' ')
        nvals = new.split(' ')

        merge = set(ovals + nvals)
        merge = ' '.join(merge)

        self.update_file_var(fname, var, merge)

    def get_file_var(self, fname, var):

        '''Get variable contents from a file'''

        with open(fname, 'r') as f:
            lines = f.readlines()
            return self.get_var_val(var, lines)

    def update_file_var(self, fname, var, value, use_quotes=True):

        '''Set variable in a file'''

        mode = 'w+'
        if os.path.exists(fname):
            mode = 'r+'

        kv = '%s=\"%s\"\n' % (var, value)
        if not use_quotes:
            kv = '%s=%s\n' % (var, value)

        with open(fname, mode) as f:
            lines = f.readlines()
            i = self.get_var_line(var, lines)
            if i is None:
                lines.append(kv)
            else:
                lines[i] = kv
            f.seek(0)
            f.writelines(lines)
            f.truncate()

    def is_mounted(self, path):

        '''Check if a given directory path is currently mounted'''

        with open('/proc/mounts', 'r') as m:
            lines = m.readlines()
            for _l in lines:
                _l = _l.split()
                if len(_l) > 1 and (_l[1] == path or _l[1] == path.rstrip('/')):
                    return True
        return False

    def get_parts_by_attr(self, attr, val=None):

        '''Find partitions that have a specified attribute'''

        parts = []
        for d in self.disks:
            for i, p in enumerate(d['partitions']):
                pidx = i + 1
                if p.get(attr):
                    if val and val != p.get(attr):
                        continue
                    if d['name'].lower().startswith('/dev/nvme'):
                        dev = '%sp%d' % (d['name'], pidx,)
                    else:
                        dev = '%s%d' % (d['name'], pidx,)
                    parts.append((d['name'], dev, p))
        return parts

    def get_volumes_by_attr(self, attr, val=None):

        '''Find logical volumes that have a specified attribute'''

        vols = []
        for _l in self.lvms:
            for vg in _l['volgroups']:
                for v in vg['volumes']:
                    attr_val = v.get(attr)
                    if attr_val:
                        if val and val != attr_val:
                            continue
                        dev = '/dev/%s/%s' % (vg['name'], v['name'])
                        vols.append((_l, vg, v, dev))
        return vols

    def get_lv_from_mount(self, mount):

        '''Find a logical volume from a specified mount location'''

        vols = self.get_volumes_by_attr('mount')
        for pv, vg, vol, volname in vols:
            mnt = vol['mount']
            if mnt == mount:
                return (pv, vg, vol, volname)

    def is_root_an_lvm(self):
        lvm = self.get_lv_from_mount('/')
        if lvm:
            return True
        return False

    def is_root_encrypted(self):
        # We need to tell the kernel if the root partition is on a crypt device
        dev = self.get_blk_dev_from_mount('/')
        if dev:
            disk, dev, part = dev
            if part.get('crypt'):
                return True
        else:
            lv = self.get_lv_from_mount('/')
            if lv:
                pv, vg, vol, vname = lv
                pv = pv.get('physvol')
                bd = self.get_blk_dev_from_mapping(self.disks, pv)
                if bd:
                    return True
        return False

    def get_blk_dev_from_mount(self, mount):

        '''Find a block device from a specified mount location'''

        parts = self.get_parts_by_attr('mount')
        for disk, dev, part in parts:
            mnt = part['mount']
            if mnt == mount:
                return (disk, dev, part)

    def get_blk_dev_from_mapping(self, disks, mapping):

        '''Find a block device from a specified mapping'''

        for d in disks:
            for i, p in enumerate(d['partitions']):
                pidx = i + 1
                crypt = p.get('crypt')
                if crypt:
                    bn = os.path.basename(mapping)
                    m = crypt.get('mapping')
                    if m and m.strip() == bn:
                        if d['name'].lower().startswith('/dev/nvme'):
                            dev = '%sp%d' % (d['name'], pidx,)
                        else:
                            dev = '%s%d' % (d['name'], pidx,)
                        return dev

    def get_uuid_for_dev(self, dev):

        '''Get the UUID for a specifed block device'''

        for d in self.get_block_attrs():
            uid = d.get('uuid')
            ndev = d.get('dev')
            if ndev == dev and uid:
                return uid

    def mount_single_device(self, mntpt, dev):

        '''Mount a block device on the default mount point'''

        base = ''
        if not self.chrooted:
            base = self.mount_point

        mnt = '%s%s' % (base, mntpt)
        if self.is_mounted(mnt):
            return

        try:
            os.mkdir(mnt)
        except FileExistsError:
            pass

        self.exec_cmd(['mount', dev, mnt])

    def mount_block_devs(self, subset=[]):

        '''Mount block devices on the default mount point'''

        parts = self.get_parts_by_attr('mount')
        for disk, dev, part in parts:
            mnt = part['mount']

            if subset and mnt not in subset:
                continue

            self.mount_single_device(mnt, dev)

        vols = self.get_volumes_by_attr('mount')
        for pv, vg, vol, dev in vols:
            mnt = vol['mount']

            if subset and mnt not in subset:
                continue

            self.mount_single_device(mnt, dev)

    def mount_rootfs(self):

        '''Mount the root file system'''

        self.mount_block_devs(subset=['/'])

    def do_chroot(self):

        '''Chroot into the mount point to continue the install'''

        if not self.chrooted:

            # Save the current root directory in case we have to leave the chroot
            if not self.realroot:
                self.realroot = os.open("/", os.O_RDONLY)

            # Copy the repo config file
            try:
                shutil.copyfile('/etc/resolv.conf',
                                self.mount_point + '/etc/resolv.conf')
            except shutil.SameFileError:
                pass

            os.chdir(self.mount_point)
            os.chroot(self.mount_point)
            self.logger.info('Entering chroot...')
            self.chrooted = True

    def update_ini_file(self, path, fields, merge=False, strict=True):

        '''Modify INI style files (commonly used in systemd)'''

        mode = 'w'

        cfg = configparser.ConfigParser(strict=strict)
        cfg.optionxform = str
        cfg.read(path)

        if merge:
            for k, v in fields.items():
                if cfg.has_section(k):
                    for z, x in v.items():
                        cfg.set(k, str(z), str(x))
                else:
                    cfg.update(fields)
        else:
            cfg.update(fields)

        with open(path, mode) as f:
            cfg.write(f)

    def read_xorg_conf(self, path):
        out_dict = OrderedDict()
        in_sect = False
        sect = None
        subsect = None
        in_subsect = False
        with open(path, 'r') as f:
            lines = f.readlines()

            for i, line in enumerate(lines):

                # Look for section header
                fields = shlex.split(line)
                vals = line.split('\"')[1::2]
                opts = [o for o in fields if o not in vals]
                if len(opts) > 1:
                    vals = []
                    copt = None
                    for f in fields[1:]:
                        if f in opts:
                            copt = f
                            vals.append(OrderedDict({copt: []}))
                        elif not copt:
                            vals.append(f)
                        else:
                            [v[copt].append(f) for v in vals if isinstance(v, dict)]

                if fields:
                    typ = fields[0].strip()
                    if not in_sect and typ == 'Section':
                        if len(fields) != 2:
                            raise InstallException('Invalid Xorg config: %s' % (path))
                        sect = fields[1].strip('\"')
                        out_dict.update(OrderedDict({sect: OrderedDict()}))
                        in_sect = True

                    elif typ == 'EndSection':
                        in_sect = False

                    elif in_sect and not in_subsect and typ == 'SubSection':
                        if len(fields) != 2:
                            raise InstallException('Invalid Xorg config: %s' % (path))
                        subsect = fields[1].strip('\"')
                        out_dict[sect].update(OrderedDict({subsect: {}}))
                        in_subsect = True

                    elif typ == 'EndSubSection':
                        in_subsect = False

                    elif in_subsect and len(fields) > 1:
                        opt = fields[0]
                        out_dict[sect][subsect].update(OrderedDict({opt: vals}))
                    elif in_sect and len(fields) > 1:
                        opt = fields[0]
                        out_dict[sect].update(OrderedDict({opt: vals}))
        return (out_dict)

    def _set_xorg_conf_section(self, path, conf):
        outlines = []
        for section, vals in conf.items():
            outlines.append('Section \"%s\"' % (section))

            if isinstance(vals, list):
                sect_vals = vals
            elif isinstance(vals, dict):
                sect_vals = [vals]
            else:
                raise InstallException('Invalid Xorg config')

            for sect_val in sect_vals:
                for opt, val in sect_val.items():
                    if isinstance(val, dict):
                        outlines.append('   SubSection \"%s\"' % (opt))
                        for o, v in val.items():
                            optline = '       %s' % (o.ljust(8))
                            for subval in v:
                                optline += '\"%s\" ' % (subval)
                            outlines.append(optline)
                        outlines.append('   EndSubSection')
                    else:
                        optline = '   %s' % (opt.ljust(12))
                        if isinstance(val, str):
                            optline += '\"%s\" ' % (val)
                        else:
                            for v in val:
                                if not isinstance(v, dict):
                                    optline += '\"%s\" ' % (v)
                                else:
                                    for subopt, subvals in v.items():
                                        optline += '%s ' % (subopt)
                                        if isinstance(subvals, list):
                                            for sv in subvals:
                                                optline += '\"%s\" ' % (sv)
                                        elif isinstance(subvals, str):
                                            optline += '\"%s\" ' % (subvals)
                                        else:
                                            raise InstallException('Invalid Xorg config')
                        outlines.append(optline)

            outlines.append('EndSection\n')
            with open(path, 'a') as f:
                f.write('\n'.join(outlines) + '\n')
            outlines = []

    def set_xorg_conf(self, path, conf):

        if os.path.exists(path):
            # Clear the config file if it already exists
            with open(path, 'w'):
                pass

        if isinstance(conf, list):
            for sect in conf:
                self._set_xorg_conf_section(path, sect)
        elif isinstance(conf, dict):
            self._set_xorg_conf_section(path, conf)
        else:
            raise InstallException('Invalid Xorg config')

    @check_chroot
    def set_display_script(self, path, lines):
        with open(path, 'r+') as f:
            curr_lines = f.readlines()
            stripped = [_l.strip() for _l in curr_lines]
            for s in stripped:
                if s in lines:
                    lines.remove(s)
            if lines:
                f.write('\n'.join(lines) + '\n')

    @check_chroot
    def setup_users(self, users=[]):

        '''Update root password and add optional users'''

        if not users:
            users = self.users

        self.logger.info('Updating root password')
        r, o = self.exec_cmd(['passwd'])

        self.logger.info('Adding user accounts')
        if users:
            for user in users:
                username = user.get('name')
                groups = user.get('groups').split()
                shell = user.get('shell')
                self.exec_cmd(['useradd', '-m', '-G', ','.join(groups), '-s', shell, username])
                self.exec_cmd(['passwd', username])

    @check_chroot
    def do_misc_config(self):
        for cfg in self.misc_config:
            path = cfg.get('path')
            if not path:
                raise InstallException('No path supplied for config file')
            sects = cfg.get('sections', {})
            for sect in sects:
                name = sect.get('name')
                for var, val in sect.get('values', []):
                    self.misc_config(path, var, val, name)

    def get_file_format(self, path):

        bn = os.path.basename(path)

        if bn in ('lvm.conf',):
            return FILE_FMT_LVM

        with open(path, 'r') as f:
            data = f.read()
            lines = data.splitlines()

            # Check for "lvm.conf" style config format
            hits = re.findall('[a-z ]+{', data)
            hits = [re.search('(?<=%s)[^}]*' % (h), data, flags=re.DOTALL) for h in hits]
            if hits:
                hits = [re.findall('\w+ = \w+\n', data) for h in hits]
                if hits:
                    return FILE_FMT_LVM

            # Check if file is in INI format
            hits = re.findall('\[[A-Za-z]+\]', data)
            if hits:
                checks = []
                for line in lines:
                    if not line.strip().startswith('#') and line in hits:
                        checks.append(line)
                if len(checks) == len(hits):
                    return FILE_FMT_INI

            # Check for "variable=value" style of config
            hits = re.findall('\S+=[^\n]*\n', data)
            if hits:
                checks = []
                for line in lines:
                    if not line.strip().startswith('#') and (line + '\n') in hits:
                        checks.append(line)
                if len(checks) == len(hits):
                    return FILE_FMT_VARVAL

        raise InstallException('Unknown file format for %s' % (path))

    @check_chroot
    def misc_config(self, path, var, val, section=''):
        def _set_lvm_vars(path, var, val, section=''):
            with open(path, 'r+') as f:
                lines = f.readlines()
                curr_sect = ''
                for i, l in enumerate(lines):
                    hit = re.search('[a-z ]+(?={)', l)
                    if hit and not l.strip().startswith('#'):

                        curr_sect = hit.group(0).strip()
                    if section and curr_sect == section:
                        if var in l and not l.strip().startswith('#'):
                            splt = l.split('=')
                            splt[1] = str(val) + '\n'
                            lines[i] = '= '.join(splt)
                            break

                    if not l.strip().startswith('#') and l.strip().startswith('}'):
                        if section and curr_sect == section:
                            lines.insert(i, '\t' + ' = '.join([str(var), str(val)]))
                            break

                f.seek(0)
                f.writelines(lines)
                f.truncate()

        def _set_ini_vars(path, var, val, section=''):
            if not section:
                raise InstallException('Need a section name to update an INI file')
            self.update_ini_file(path, {section: {var: val}}, merge=True)

        def _set_var_val(path, var, val, section=''):
            self.update_file_var(path, var, val)

        fmt = self.get_file_format(path)
        if fmt == FILE_FMT_LVM:
            _set_lvm_vars(path, var, val, section)
        elif fmt == FILE_FMT_INI:
            _set_ini_vars(path, var, val, section)
        elif fmt == FILE_FMT_VARVAL:
            _set_var_val(path, var, val, section)
