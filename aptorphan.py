#! /usr/bin/env python3

import sys

import apt_pkg
import ast

class Dict(dict):
    def compute_if_absent(self, key, mapping):
        try:
            return self[key]
        except KeyError:
            return self.setdefault(key, mapping(key))

class Repository(object):
    def __init__(self):
        apt_pkg.init()
        cache = apt_pkg.Cache(progress=None)
        depcache = apt_pkg.DepCache(cache)
        self.find_packages = lambda: cache.packages
        self.find_candidate_version = depcache.get_candidate_ver
        self.find_package_by_name = lambda name: cache[name]
        self.is_auto_installed = depcache.is_auto_installed

class Wrapper(object):
    def __init__(self, underlying):
        self.underlying = underlying # XXX: make private
    def __getattr__(self, name):
        try:
            return getattr(self.underlying, name)
        except AttributeError:
            value = self.__class__.defaults[name]()
            setattr(self, name, value)
            return value

class Package(Wrapper):
    defaults = {
        'candidate_version': lambda: None,
        'rank': lambda: None,
        'hint': lambda: None,
        }

class Version(Wrapper):
    defaults = {
        'is_candidate_version': lambda: False,
        'rank': lambda: None,
        'conflicts': lambda: set(),
        'unresolved': lambda: [],
        'notify': lambda: set(),
        'replaced_by': lambda: set(),
        }

class Manager(object):
    def __init__(self, repository):
        self.__repository = repository
        self.__foreign = frozenset(apt_pkg.get_architectures()[1:])
        self.__packages = Dict()
        self.__versions = Dict()
        self.__resolve = {
            'Conflicts': self.__resolve_conflicts,
            'Depends': self.__resolve_depends,
            'Breaks': self.__resolve_conflicts,
            'Enhances': self.__resolve_ignore, # XXX
            'Obsoletes': self.__resolve_ignore, # XXX
            'PreDepends': self.__resolve_depends,
            'Recommends': self.__resolve_depends,
            'Replaces': self.__resolve_replaces,
            'Suggests': self.__resolve_ignore,
            }
        self.__ignore_forward = { 'Enhances', }
        self.__ignore_backward = { 'Depends', 'Recommends', 'Suggests', 'PreDepends', }
        self.__rank = 0
        # Initialize the candidate version for each package. The
        # information is required in __find_base_versions, so it makes
        # no sense to do it lazily.
        for p in map(self.wrapped_package, self.__repository.find_packages()):
            if p.has_versions:
                v = self.wrapped_version(self.__repository.find_candidate_version(p.underlying))
                p.candidate_version = v
                v.is_candidate_version = True
        # Automatically rank all base packages.
        self.rank(self.__find_base_versions(), 'D')
    def wrapped_package(self, package):
        return self.__packages.compute_if_absent(package.id, lambda id: Package(package))
    def wrapped_version(self, version):
        return self.__versions.compute_if_absent(version.id, lambda id: Version(version))
    def __find_base_versions(self):
        # The set of base packages consists of all packages with a
        # priority of either REQUIRED, IMPORTANT or STANDARD. The
        # latter is questionable. However, for desktop systems it
        # avoids a few surprises regarding missing commands.
        #
        # Note: There is also a member variable 'essential'. This
        # variable may be interesting, but there are two reasons why
        # this flag is currently not used: (1) Apparently all
        # essential packages are REQUIRED or IMPORTANT (2) What
        # happens if there are several versions with different values.
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
        package = self.__repository.find_package_by_name(package_name)
        if package.has_versions:
            self.rank([ self.wrapped_version(self.__repository.find_candidate_version(package)) ], hint)
        elif package.has_provides and len(package.provides_list) == 1:
            self.rank([ self.wrapped_version(package.provides_list[0][2]) ], hint)
        else:
            raise Exception('can not rank package without versions: {}'.format(package_name))
    def rank(self, versions, hint):
        while versions:
            versions = sorted(versions, key=lambda v: v.parent_pkg.name)
            versions = self.rank_once(versions, hint)
            hint = None
    def rank_once(self, versions, hint):
        self.__rank += 1
        self.__pending_depends = set(versions)
        for v in versions:
            if v.rank is None:
                v.rank = self.__rank
                v.hint = hint
                for kind, and_group in v.depends_list.items():
                    for or_group in and_group:
                        self.__resolve[kind](v, or_group)
                self.__pending_depends.update(v.notify)
        result = set()
        while self.__pending_depends:
            v = self.__pending_depends.pop()
            i = 0
            while i < len(v.unresolved):
                targets = self.__expand_or_group(v.unresolved[i])
                candidate = self.__resolve_once(targets, False)
                if any(target.rank for target in targets):
                    # already resolved and nothing more to do
                    del v.unresolved[i]
                elif candidate:
                    result.add(candidate)
                    del v.unresolved[i]
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
    def __resolve_depends(self, version, or_group):
        version.unresolved.append(or_group)
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
        # keep the original order
        result = []
        seen = set()
        for dependency in or_group:
            for target in map(self.wrapped_version, dependency.all_targets()):
                if target not in seen:
                    result.append(target)
                    seen.add(target)
        return result
    def __format_or_group(self, or_group):
        def make(dep):
            p = self.__format_package(self.wrapped_package(dep.target_pkg))
            return '{}{}{}'.format(p, dep.comp_type, dep.target_ver)
        return ' | '.join(map(make, or_group))
    def __is_installed_package(self, package):
        return (package.current_state != apt_pkg.CURSTATE_NOT_INSTALLED
                and package.selected_state != apt_pkg.SELSTATE_UNKNOWN)
    def __is_interessting_package(self, package):
        return package.rank is not None or self.__is_installed_package(package)
    def __is_interessting_version(self, version):
        return version.is_candidate_version and self.__is_interessting_package(self.wrapped_package(version.parent_pkg))
    def rank_unresolved(self):
        resolved = True
        while resolved:
            resolved = False
            for v in list(self.__versions.values()):
                if v.rank and v.unresolved:
                    for or_group in list(v.unresolved):
                        targets = self.__expand_or_group(or_group)
                        candidate = self.__resolve_once(targets, True)
                        if not any(target.rank for target in targets) and candidate is not None:
                            self.rank([ candidate ], 'C')
                            resolved = True
    def dump_unresolved(self):
        for v in self.__versions.values():
            if v.rank and v.unresolved:
                for or_group in v.unresolved:
                    make = self.__format_version
                    print('UNRESOLVED: {} => {} ({})'.format(
                            make(v),
                            self.__format_or_group(or_group),
                            ' | '.join(map(make, self.__expand_or_group(or_group)))))
        for v in self.__versions.values():
            p = self.wrapped_package(v.parent_pkg)
            if v.rank is None:
                pass # nothing
            elif p.rank is None:
                p.rank = v.rank
                p.hint = v.hint
            else:
                raise Exception('unexpected ranked versions: {}'.format(p.get_fullname()))
        # The remaining packages are sorted by the field
        # AUTO_INSTALLED. This order is usually good enough to spot,
        # which packages should be actually removed from the system.
        removes = []
        for p in self.__packages.values():
            if p.rank is None:
                if p.current_state != apt_pkg.CURSTATE_NOT_INSTALLED:
                    score = self.__repository.is_auto_installed(p.underlying)
                    removes.append((p, score))
            elif (p.current_state != apt_pkg.CURSTATE_INSTALLED
                  and p.selected_state != apt_pkg.SELSTATE_INSTALL):
                print('INSTALL:', self.__format_package(p))
                self.__dump_dependencies(p)
            elif p.current_ver.id != p.candidate_version.id:
                print('UPGRADE:', self.__format_package(p))
                self.__dump_dependencies(p)
            elif self.__repository.is_auto_installed(p.underlying) == (p.hint == 'W'):
                print('WISHLIST:', self.__format_package(p))
        for p, score in sorted(removes, key=lambda t: (t[1], t[0].name)):
            print('REMOVE:', self.__format_package(p))
            self.__dump_dependencies(p)
    def __dump_dependencies(self, package):
        v = package.candidate_version
        for reverse in filter(self.__is_interessting_package, { self.wrapped_package(dependency.parent_pkg) for dependency in package.rev_depends_list }):
            self.__dump_dependencies_forward(reverse.candidate_version, v)
        self.__dump_dependencies_backward(v)
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
        if self.__repository.is_auto_installed(package.underlying):
            items.append('M')
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
        return self.__format_package(self.wrapped_package(version.parent_pkg))

if __name__ == '__main__':
    parse = lambda text: ast.literal_eval(text)
    wishlist = []
    for pathname in sys.argv[1:]:
        with open(pathname, 'r') as f:
            wishlist.extend(parse(f.read()))
    manager = Manager(Repository())
    for package_name in wishlist:
        manager.rank_by_name(package_name, 'W')
    manager.rank_unresolved()
    manager.dump_unresolved()
