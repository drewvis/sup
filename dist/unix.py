import os
import json
import logging


# Disable logger exceptions
logging.raiseExceptions = False


class InstallException(Exception):
    pass


class CmdError(InstallException):
    def __init__(self, error, code, output):
        self.code = code
        self.error = error
        self.output = output


def get_logger(logpath):

    '''Sets up the installation logger'''

    logger = logging.getLogger('sup')
    logger.setLevel(logging.DEBUG)
    sh = logging.StreamHandler()
    fh = logging.FileHandler(logpath, 'a', encoding='utf-8')

    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    sh.setFormatter(fmt)
    fh.setFormatter(fmt)

    logger.addHandler(sh)
    logger.addHandler(fh)

    return logger


class UnixInstaller(object):
    '''Generic Unix-like installer'''

    def __init__(self, config, logpath='sup.log'):

        self.bootdev = None
        self.realroot = None
        self.logger = get_logger(logpath)

        if not os.path.exists(config):
            raise InstallException('Install config file not found: %s' % config)
        with open(config, 'r') as f:
            self.config = json.load(f)
