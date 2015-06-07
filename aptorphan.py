#! /usr/bin/env python3

import sys

import apt_pkg
import ast


class Cache(object):
    def __init__(self):
        apt_pkg.init()
        self.__cache = apt_pkg.Cache(progress=None)
        self.__depcache = apt_pkg.DepCache(self.__cache)
    def find_candidate_version(self, package):
        return self.__depcache.get_candidate_ver(package)
    def find_package_by_name(self, name):
        return self.__cache[name]
    def find_packages(self):
        return self.__cache.packages
    def is_auto_installed(self, package):
        return self.__depcache.is_auto_installed(package)
    def is_garbage(self, package):
        return self.__depcache.is_garbage(package)


def magic_getattr(object, name, defaults):
    try:
        return getattr(object.base, name)
    except AttributeError:
        value = defaults[name]()
        setattr(object, name, value)
        return value

def map_insert(m, k, f):
    try:
        return m[k]
    except KeyError:
        return m.setdefault(k, f())

class Package(object):
    defaults = {
        'candidate_version': lambda: None,
        'rank': lambda: None,
        'hint': lambda: None,
        }
    def __init__(self, base):
        self.base = base
    def __getattr__(self, name):
        return magic_getattr(self, name, self.defaults)


class Version(object):
    defaults = {
        'is_candidate_version': lambda: False,
        'rank': lambda: None,
        'conflicts': lambda: set(),
        'unresolved': lambda: [],
        'notify': lambda: set(),
        'replaced_by': lambda: set(),
        }
    def __init__(self, base):
        self.base = base
    def __getattr__(self, name):
        return magic_getattr(self, name, self.defaults)


class Manager(object):
    def __init__(self, cache):
        self.__cache = cache
        self.__foreign = frozenset(apt_pkg.get_architectures()[1:])
        self.__packages = {}
        self.__versions = {}
        self.__resolve = {
            'Conflicts': self.__resolve_conflicts,
            'Depends': self.__resolve_depends,
            'Breaks': self.__resolve_conflicts,
            'Enhances': self.__resolve_ignore, # XXX
            'Obsoletes': self.__resolve_ignore, # XXX
            'PreDepends': self.__resolve_depends,
            'Recommends': self.__resolve_recommends,
            'Replaces': self.__resolve_replaces,
            'Suggests': self.__resolve_ignore,
            }
        self.__ignore_forward = { 'Enhances', }
        self.__ignore_backward = { 'Depends', 'Recommends', 'Suggests', 'PreDepends', }
        self.__rank = 0
        for package in map(self.package, self.__cache.find_packages()):
            if package.has_versions:
                version = self.version(self.__cache.find_candidate_version(package.base))
                package.candidate_version = version
                version.is_candidate_version = True
        self.rank(self.__find_default_versions(), 'D')
    def package(self, package):
        return map_insert(self.__packages, package.id, lambda: Package(package))
    def version(self, version):
        return map_insert(self.__versions, version.id, lambda: Version(version))
    def __find_default_versions(self):
        # Packages have an member variable 'essential'. This variable may be
        # interesting, but there are two reasons why this flag is currently
        # not used: (1) Apparently all essential packages are REQUIRED or
        # IMPORTANT (2) What happens if there are several versions with
        # different values.
        priorities = { apt_pkg.PRI_REQUIRED, apt_pkg.PRI_IMPORTANT, apt_pkg.PRI_STANDARD, }
        for package in self.__packages.values():
            if package.has_versions:
                version = package.candidate_version
                if version.priority not in priorities:
                    pass # skip optional packages
                elif version.arch in self.__foreign:
                    pass # skip default packages of foreign architectures
                else:
                    yield version
        raise StopIteration
    def rank_by_name(self, package_name, hint):
        package = self.__cache.find_package_by_name(package_name)
        if package.has_versions:
            self.rank({ self.version(self.__cache.find_candidate_version(package)) }, hint)
        elif package.has_provides and len(package.provides_list) == 1:
            self.rank({ self.version(package.provides_list[0][2]) }, hint)
        else:
            raise Exception('can not rank package without versions: {}'.format(package_name))
    def rank(self, versions, hint):
        while versions:
            versions = sorted(versions, key = lambda version: version.parent_pkg.name)
            versions = self.rank_once(versions, hint)
            hint = None
    def rank_once(self, versions, hint):
        self.__rank += 1
        self.__pending_depends = set(versions)
        for version in versions:
            if version.rank is None:
                version.rank = self.__rank
                version.hint = hint
                for kind, and_group in version.depends_list.items():
                    for or_group in and_group:
                        self.__resolve[kind](version, or_group)
                self.__pending_depends.update(version.notify)
            #elif hint:
            #    print('CONFIG[{}]: {}'.format(hint, self.__format_package(self.package(version.parent_pkg))))
        result = set()
        while self.__pending_depends:
            version = self.__pending_depends.pop()
            i = 0
            while i < len(version.unresolved):
                optional, or_group = version.unresolved[i]
                targets = self.__expand_or_group(or_group)
                candidate = self.__resolve_once(targets, False)
                if any(target.rank for target in targets):
                    # already resolved and nothing more to do
                    del version.unresolved[i]
                elif candidate:
                    result.add(candidate)
                    del version.unresolved[i]
                else:
                    # not resolved
                    i += 1
        return result
    def __resolve_once(self, targets, use_first):
        candidates = [ t for t in targets if t.is_candidate_version ]
        preferred = [ c for c in candidates if not c.conflicts ]
        if len(preferred) == 1 or (len(preferred) > 1 and use_first):
            return preferred[0]
        if len(candidates) == 1 or (len(candidates) > 1 and use_first):
            return candidates[0]
        return None
    def __resolve_depends_and_recommends(self, version, or_group, optional):
        version.unresolved.append((optional, or_group))
        for target in self.__expand_or_group(or_group):
            target.notify.add(version)
    def __resolve_depends(self, version, or_group):
        self.__resolve_depends_and_recommends(version, or_group, False)
    def __resolve_depends(self, version, or_group):
        self.__resolve_depends_and_recommends(version, or_group, True)
    def __resolve_recommends(self, version, or_group):
        version.unresolved.append((True, or_group))
        for target in self.__expand_or_group(or_group):
            target.notify.add(version)
    def __resolve_conflicts(self, version, or_group):
        if len(or_group) != 1:
            raise Exception('unexpected conflicts: {} {}'.format(version.parent_pkg.get_fullname(), or_group))
        for target in self.__expand_or_group(or_group):
            version.conflicts.add(target)
            target.conflicts.add(version)
            for subject in target.notify:
                if subject.unresolved:
                    self.__pending_depends.add(subject)
    def __resolve_replaces(self, version, or_group):
        if len(or_group) != 1:
            raise Exception('unexpected replaces: {} {}'.format(version.parent_pkg.get_fullname(), or_group))
        for target in self.__expand_or_group(or_group):
            target.replaced_by.add(version)
    def __resolve_ignore(self, version, or_group):
        pass
    def __expand_or_group(self, or_group):
        result = []
        seen = set()
        for dependency in or_group:
            for target in map(self.version, dependency.all_targets()):
                if target not in seen:
                    result.append(target)
                    seen.add(target)
        return result
    def __format_or_group(self, or_group):
        def make(dep):
            p = self.__format_package(self.package(dep.target_pkg))
            return '{}{}{}'.format(p, dep.comp_type, dep.target_ver)
        return ' | '.join(map(make, or_group))
    def __is_installed_package(self, package):
        return (package.current_state != apt_pkg.CURSTATE_NOT_INSTALLED
                and package.selected_state != apt_pkg.SELSTATE_UNKNOWN)
    def __is_interessting_package(self, package):
        return package.rank is not None or self.__is_installed_package(package)
    def __is_interessting_version(self, version):
        return version.is_candidate_version and self.__is_interessting_package(self.package(version.parent_pkg))
    def rank_unresolved(self):
        resolved = True
        while resolved:
            resolved = False
            for version in list(self.__versions.values()):
                if version.rank and version.unresolved:
                    for optional, or_group in list(version.unresolved):
                        targets = self.__expand_or_group(or_group)
                        candidate = self.__resolve_once(targets, True)
                        if not any(target.rank for target in targets) and candidate is not None:
                            self.rank({ candidate, }, 'C')
                            resolved = True
    def dump_unresolved(self):
        for version in self.__versions.values():
            if version.rank and version.unresolved:
                for optional, or_group in version.unresolved:
                    make = self.__format_version
                    print('UNRESOLVED: {} => {} ({})'.format(
                            make(version),
                            self.__format_or_group(or_group),
                            ' | '.join(map(make, self.__expand_or_group(or_group)))))
        for version in self.__versions.values():
            package = self.package(version.parent_pkg)
            if version.rank is None:
                pass # nothing
            elif package.rank is None:
                package.rank = version.rank
                package.hint = version.hint
            else:
                raise Exception('unexpected ranked versions: {}'.format(package.get_fullname()))
        # Die zu entfernenden Pakete werden nach AUTO_INSTALLED sortiert.
        # Diese Sortierung reicht in der Regel bereits aus, damit der
        # Anwender einfach sieht, weil Pakete er tatsaechlich entfernen
        # beziehungsweise als erwuenscht markieren muss.
        removes = []
        for package in self.__packages.values():
            if package.rank is None:
                if package.current_state != apt_pkg.CURSTATE_NOT_INSTALLED:
                    score = self.__cache.is_auto_installed(package.base)
                    removes.append((package, score))
            elif (package.current_state != apt_pkg.CURSTATE_INSTALLED
                  and package.selected_state != apt_pkg.SELSTATE_INSTALL):
                print('INSTALL:', self.__format_package(package))
                self.__dump_dependencies(package)
            elif package.current_ver.id != package.candidate_version.id:
                print('UPGRADE:', self.__format_package(package))
                self.__dump_dependencies(package)
            elif self.__cache.is_auto_installed(package.base) == (package.hint == 'W'):
                print('WISHLIST:', self.__format_package(package))
        for package, score in sorted(removes, key=lambda t: (t[1], t[0].name)):
            print('REMOVE:', self.__format_package(package))
            self.__dump_dependencies(package)
    def __dump_dependencies(self, package):
        version = package.candidate_version
        for reverse in filter(self.__is_interessting_package, { self.package(dependency.parent_pkg) for dependency in package.rev_depends_list }):
            self.__dump_dependencies_forward(reverse.candidate_version, version)
        self.__dump_dependencies_backward(version)
    def __dump_dependencies_forward(self, source, target):
        for kind, and_group in source.depends_list.items():
            if kind not in self.__ignore_forward:
                for or_group in and_group:
                    if target in self.__expand_or_group(or_group):
                        print('  {} {}: {}'.format(self.__format_version(source), kind.lower(), self.__format_or_group(or_group)))
    def __dump_dependencies_backward(self, version):
        for kind, and_group in version.depends_list.items():
            if kind not in self.__ignore_backward:
                for or_group in and_group:
                    if any(map(self.__is_interessting_version, self.__expand_or_group(or_group))):
                        print('  {} {}: {}'.format(self.__format_version(version), kind.lower(), self.__format_or_group(or_group)))
    def __format_package(self, package):
        items = []
        if self.__cache.is_auto_installed(package.base):
            items.append('M')
        if self.__cache.is_garbage(package.base):
            items.append('G')
        if package.current_state == apt_pkg.CURSTATE_CONFIG_FILES:
            items.append('c')
        elif package.current_state != apt_pkg.CURSTATE_NOT_INSTALLED:
            items.append('i')
        if package.has_provides and not package.has_versions:
            items.append('v')
        if package.hint is not None:
            items.append(package.hint)
        if package.rank is not None:
            items.append(package.rank)
        items = ''.join(map(str, items))
        if items:
            return '{}[{}]'.format(package.get_fullname(pretty=True), items)
        else:
            return package.get_fullname(pretty=True)
    def __format_version(self, version):
        return self.__format_package(self.package(version.parent_pkg))

if __name__ == '__main__':
    if len(sys.argv) >= 2:
        wishlist = ''.join(open(pathname).read() for pathname in sys.argv[1:])
    else:
        wishlist = sys.stdin.read()
    wishlist = ast.literal_eval(wishlist)
    manager = Manager(Cache())
    for package_name in wishlist:
        manager.rank_by_name(package_name, 'W')
    manager.rank_unresolved()
    manager.dump_unresolved()
