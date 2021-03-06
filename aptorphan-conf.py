#! /usr/bin/env python3

# Create an initial configuration file for aptorphan based on the
# currently installed packages, that are not marked as automatically
# installed.
#
# For most setups it should provide a good starting point. However, if
# there are too many manually installed packages, it might be
# worthwhile doing some preparations in aptitude:
#
#
# (1) Change preferences:
#     Packages that should never be automatically removed:
#     "?or(~pimportant,~pstandard)"
#
# (2) Group packages by status ('G'): "status"
#
# (3) Mark standard packages as automatically installed:
#     Package tree limit ('l'): "?or(~pimportant,~pstandard) ~i !~M"
#     Mark packages as being automatically installed ('M').
#
# (4) Mark unfamiliar packages as automatically installed:
#     Package tree limit ('l'): "~i !~M"
#     For each unfamiliar package: 'M'

import sys

import apt_pkg

class Dict(dict):
    def compute_if_absent(self, key, mapping):
        try:
            return self[key]
        except KeyError:
            pass # unwind exception stack
        return self.setdefault(key, mapping(key))

class Repository(object):
    def __init__(self):
        apt_pkg.init()
        cache = apt_pkg.Cache(progress=None)
        depcache = apt_pkg.DepCache(cache)
        self.find_packages = lambda: cache.packages
        self.is_auto_installed = depcache.is_auto_installed

if __name__ == '__main__':
    repository = Repository()
    priorities = {apt_pkg.PRI_REQUIRED, apt_pkg.PRI_IMPORTANT, apt_pkg.PRI_STANDARD}
    sections = Dict()
    for p in repository.find_packages():
        v = p.current_ver
        if v and (v.priority not in priorities) and not repository.is_auto_installed(p):
            pf = v.file_list[0][0]
            names = sections.compute_if_absent(
                '{}: {}/{}'.format(pf.origin, v.section, pf.component),
                lambda section: [])
            names.append(p.get_fullname(pretty=True))
    sys.stdout.write('{\n')
    for section, names in sorted(sections.items()):
        sys.stdout.write('\n    # {}\n'.format(section))
        sys.stdout.write(''.join(map('    {!r},\n'.format, sorted(names))))
    sys.stdout.write('\n}\n')
