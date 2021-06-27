# sup
Simple Unix-like Provisioner

sup aims to be a common interface for installing and provisioning Unix-like OS distributions. Often, more customizable distros (Gentoo, Arch, etc.) will require a great deal of time and knowledge to get in a functional state. sup differs from solutions such as Ansible playbooks by not relying on code or commands to be inserted into configs. Complex distributions may update installation steps or change the location of URLs or public keys. This leads to a broken configuration file that then needs to be updated by the user. The installation code should know the current best way to install a given distribution while being agnostic of what configs are being used for install.

## Support
Currently supports installations of Arch Linux and Gentoo Linux. All installs were tested on a SystemRescue ISO image.

## Usage
Install scripts currently exist for Arch (`install_arch.py`) and Gentoo (`install_gentoo.py`) respectively. An example of how to install Gentoo linux using a custom sup build config along with a custom kernel kconfig is show below:
`
python3 install_gentoo.py -f builds/vmware/gentoo/vmware_lvm.cfg -k builds/vmware/kconfigs/openrc.kfg --prepdisks --getstage --setupenv --config-portage --sysconf --update-world --kernel --fstab --packages --services --bootloader --network --users --display
`

Note: Do not run install scripts against block devices that you do want to erase; IT WILL FORMAT THE DEVICE AND ERASE ALL YOUR DATA.

(work in progress)
