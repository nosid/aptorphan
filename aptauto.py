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

class Cache(object):
    def __init__(self):
        apt_pkg.init()
        cache = apt_pkg.Cache(progress=None)
        depcache = apt_pkg.DepCache(cache)
        self.find_packages = lambda: cache.packages
        self.is_auto_installed = depcache.is_auto_installed

class Wrapper(object):
    def __init__(self, underlying):
        self.underlying = underlying
    def __getattr__(self, name):
        try:
            return getattr(self.underlying, name)
        except AttributeError:
            value = self.__class__.defaults[name]()
            setattr(self, name, value)
            return value

class Version(Wrapper):
    defaults = {
        'reverse': lambda: set(),
    }

class Manager(object):
    def __init__(self, cache):
        self.__cache = cache
        self.__versions = Dict()
        self.__resolve = {
            'Conflicts': self.__resolve_ignore,
            'Depends': self.__resolve_depends,
            'Breaks': self.__resolve_ignore,
            'Enhances': self.__resolve_ignore,
            'Obsoletes': self.__resolve_ignore,
            'PreDepends': self.__resolve_depends,
            'Recommends': self.__resolve_depends,
            'Replaces': self.__resolve_ignore,
            'Suggests': self.__resolve_ignore,
        }
        versions = set()
        for p in self.__cache.find_packages():
            if p.current_ver:
                versions.add(self.wrapped_version(p.current_ver))
        versions = sorted(versions, key=lambda v: v.parent_pkg.name)
        for v in versions:
            for kind, and_group in v.depends_list.items():
                for or_group in and_group:
                    self.__resolve[kind](v, or_group)
        for v in versions:
            if not self.__cache.is_auto_installed(v.parent_pkg) and v.reverse:
                print('AUTO: {}'.format(v.parent_pkg.name))
    def wrapped_version(self, version):
        return self.__versions.compute_if_absent(version.id, lambda id: Version(version))
    def __resolve_depends(self, version, or_group):
        for target in self.__expand_or_group(or_group):
            if target.parent_pkg.current_ver and self.wrapped_version(target.parent_pkg.current_ver) == target:
                target.reverse.add(version)
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

if __name__ == '__main__':
    Manager(Cache())
