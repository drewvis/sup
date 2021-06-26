# sup
Simple Unix-like Provisioner

sup aims to be a common interface for installing and provisioning Unix-like OS distributions. Often, more customizable distros (Gentoo, Arch, etc.) will require a great deal of time and knowledge to get in a functional state. sup differs from solutions such as Ansible playbooks by not relying on code or commands to be inserted into configs. Complex distributions may update installation steps or change the location of URLs or public keys. This leads to a broken configuration file that then needs to be updated by the user. The installation code should know the current best way to install a given distribution while being agnostic of what configs are being used for install.

(work in progress)
