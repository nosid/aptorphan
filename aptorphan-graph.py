#! /usr/bin/env python3

import sys

import apt_pkg
import ast

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
        self.find_candidate_version = depcache.get_candidate_ver
        self.find_package_by_name = lambda name: cache[name] if name in cache else None
        self.is_auto_installed = depcache.is_auto_installed

class Global(object):
    priorities = {'required', 'important', 'standard'}
    depends = {'Depends', 'PreDepends', 'Recommends'}
    conflicts = {'Breaks', 'Conflicts'} # 'Replaces' is more a hint than a conflict

class Model(object):

    class Impl(object):
        def __init__(self, repository):
            self.repository = repository
            self.packages = Dict()
            self.versions = Dict()
        def package(self, package):
            return self.packages.compute_if_absent(package.id, lambda id: Model.Package(self, package))
        def version(self, version):
            return self.versions.compute_if_absent(version.id, lambda id: Model.Version(self, version))

    class Package(object):
        def __init__(self, impl, underlying):
            self.__impl = impl
            self.__underlying = underlying
        def display_name(self):
            return self.__underlying.get_fullname(pretty=True)

    class Version(object):
        def __init__(self, impl, underlying):
            self.__impl = impl
            self.__underlying = underlying
        def package(self):
            return self.__impl.package(self.__underlying.parent_pkg)
        def is_candidate_version(self):
            return self.__impl.repository.find_candidate_version(
                self.__underlying.parent_pkg).id == self.__underlying.id
        __suppress_empty_dependency = {'Conflicts', 'Replaces', 'Breaks', 'Suggests', 'Enhances', 'Recommends'}
        def relates(self, kinds=None):
            for kind, and_group in self.__underlying.depends_list.items():
                if kinds is None or kind in kinds:
                    for or_group in and_group:
                        targets = self.__expand_or_group(or_group)
                        if targets:
                            yield kind, targets
                        elif kind not in Model.Version.__suppress_empty_dependency:
                            raise Exception('invalid dependency', self.display_name(), kind, or_group)
        def __expand_or_group(self, or_group):
            # keep the original order
            result = []
            seen = set()
            for dependency in or_group:
                for target in map(self.__impl.version, dependency.all_targets()):
                    if target not in seen:
                        result.append(target)
                        seen.add(target)
            return result
        def id(self):
            return self.__underlying.id
        def display_name(self):
            return self.__underlying.parent_pkg.get_fullname(pretty=True)

    def __init__(self, repository):
        self.__impl = Model.Impl(repository)
    def find_installed_versions(self):
        result = []
        for p in self.__impl.repository.find_packages():
            v = p.current_ver
            if v is not None:
                result.append(v)
        # sort the versions by name to make the result deterministic
        return map(self.__impl.version, sorted(result, key=lambda v: v.parent_pkg.name))
    def find_versions_by_priority(self, priority_name):
        priority = {
            'required':apt_pkg.PRI_REQUIRED,
            'important':apt_pkg.PRI_IMPORTANT,
            'standard':apt_pkg.PRI_STANDARD,
            'optional':apt_pkg.PRI_OPTIONAL,
            'extra':apt_pkg.PRI_EXTRA,
        }[priority_name]
        foreign = frozenset(apt_pkg.get_architectures()[1:])
        result = []
        for p in self.__impl.repository.find_packages():
            if p.has_versions:
                v = self.__impl.repository.find_candidate_version(p)
                if v and (v.priority == priority) and (v.arch not in foreign):
                    result.append(v)
        # sort the versions by name to make the result deterministic
        return map(self.__impl.version, sorted(result, key=lambda v: v.parent_pkg.name))
    def find_candidate_version_by_name(self, name):
        p = self.__impl.repository.find_package_by_name(name)
        if p is None:
            raise Exception('unknown package: {}'.format(name))
        if p.has_versions:
            return self.__impl.version(self.__impl.repository.find_candidate_version(p))
        if not p.has_provides:
            raise Exception('invalid package: {}'.format(name))
        if len(p.provides_list) != 1:
            raise Exception('virtual package: {}'.format(name))
        return self.__impl.version(p.provides_list[0][2])

class Resolver(object):
    def __init__(self, versions=Dict(), conflicts=set()):
        self.__versions = versions
        self.__conflicts = conflicts
        self.__pending = []
    def put(self, version):
        def handler(source):
            for kind, targets in version.relates(Global.depends):
                self.__pending.append((source, targets))
            for kind, targets in version.relates(Global.conflicts):
                self.__conflicts.update(targets)
        self.__versions.compute_if_absent(version, handler)
    def __sort_pending(self):
        self.__pending.sort(key=lambda t: t[0].package().display_name())
    def __take_pending(self):
        self.__pending, result = [], self.__pending
        return result
    def resolve(self):
        while True:
            while self.__resolve_all_trivial_targets():
                pass
            if self.__resolve_one_designated_target(Model.Version.is_candidate_version):
                continue
            elif self.__resolve_one_designated_target(lambda v: True):
                continue
            else:
                return frozenset(self.__versions)
    def __resolve_all_trivial_targets(self):
        progress = False
        self.__sort_pending() # sort for deterministic behavior
        for source, targets in self.__take_pending():
            if any(v in self.__versions for v in targets):
                continue # already fulfilled
            targets = [t for t in targets if t not in self.__conflicts]
            if len(targets) == 0:
                pass # silently ignore unresolvable dependency
            elif len(targets) > 1:
                self.__pending.append((source, targets))
            elif targets[0].is_candidate_version():
                self.put(targets[0])
                progress = True
            else:
                self.__pending.append((source, targets))
        return progress
    def __resolve_one_designated_target(self, predicate):
        counter = {}
        for source, targets in self.__pending:
            targets = [t for t in targets if predicate(t)]
            if targets:
                target = targets[0]
                counter[target] = counter.get(target, 0) + 1
        if counter:
            candidate = sorted(counter.items(), key=lambda item: (-item[1], item[0].package().display_name()))[0][0]
            self.put(candidate)
            return True
        return False

if __name__ == '__main__':

    # Step 1: Find all versions which are expected to be
    # installed. This step completely ignores whether the version is
    # currently installed or not.

    # Explicits: All versions directly configured by the user in a
    # configuration file passed to the application. Note that a
    # version might be configured in more than one configuration file.
    explicits = Dict()
    for pathname in sys.argv[1:]:
        filename = pathname.split('/')[-1]
        with open(pathname, 'r') as f:
            for name in ast.literal_eval(f.read()):
                explicits.compute_if_absent(name, lambda name: []).append(filename)

    # Late initialization of repository, so that syntax errors in the
    # configuration files can be reported immediately.
    model = Model(Repository())
    explicits = {model.find_candidate_version_by_name(name):filenames
                 for name, filenames in sorted(explicits.items())}

    # Implicits: All versions with high priority. These versions
    # should always be installed in a standard setup.
    implicits = {version:priority
                 for priority in Global.priorities
                 for version in model.find_versions_by_priority(priority)}

    # Inference: Starting with the explicitly and implicitly selected
    # versions, recursively infer further versions that are expected
    # to be installed based on their dependencies.
    resolver = Resolver()
    for sets in (implicits, explicits):
        for version in sets:
            resolver.put(version)
    expected = set(resolver.resolve())

    # Step 2: Find all versions, which are actually installed, and
    # determine the difference between the actual and the expected
    # set.
    actual = set(model.find_installed_versions())
    missing = expected - actual
    spurious = actual - expected

    # Step 3: Find anchor versions from the intersection of expected
    # and actual. These versions will be shown in addition to missing
    # and spurious versions, because these versions explain, why a
    # missing version should actually be installed.
    def find_parents(candidates, children):
        for candidate in candidates:
            for kind, targets in candidate.relates(Global.depends):
                if any(target in children for target in targets):
                    yield candidate
    anchors = set()
    anchors_aux = set(find_parents(expected & actual, missing))
    while anchors_aux:
        anchors.update(anchors_aux)
        anchors_aux = set(find_parents((expected & actual) - anchors, anchors_aux - implicits.keys() - explicits.keys()))

    candidates = {target for anchor in anchors for kind, targets in anchor.relates(Global.depends) for target in targets if target in missing}

    versions = {}
    versions.update(dict.fromkeys(anchors, 'anchor'))
    versions.update(dict.fromkeys(missing, 'missing'))
    versions.update(dict.fromkeys(spurious, 'spurious'))

    write = lambda format, *args: sys.stdout.write(format.format(*args))

    def make_raw_node(id, **kwargs):
        write('    "{}" [{}];\n', id,
              ','.join('{}="{}"'.format(key, value) for key, value in sorted(kwargs.items())))

    def make_raw_edge(source_id, target_id, **kwargs):
        write('    "{}" -> "{}" [{}];\n', source_id, target_id,
              ','.join('{}="{}"'.format(key, value) for key, value in sorted(kwargs.items())))

    def make_edge(source, target, kind, source_id=None, **kwargs):
        arrowhead, style = {
            'Depends': ('normal', 'solid'),
            'Recommends': ('empty', 'solid'),
            'Suggests': ('empty', 'dotted'),
            }[kind]
        if versions[source] != versions[target]:
            style = 'bold'
        make_raw_edge(source_id or source.id(), target.id(), label=kind, arrowhead=arrowhead, style=style, **kwargs)

    def make_edges(source, kinds, color):
        for seq, (kind, targets) in enumerate(source.relates(kinds)):
            filtered = [target for target in targets if target in versions and target != source]
            if not filtered:
                pass
            elif len(targets) > 1:
                fork_id = '{}:{}'.format(source.id(), seq)
                make_raw_node(fork_id, label='', shape='point', fixedsize=True, width=0.1, height=0.1, color=color)
                make_raw_edge(source.id(), fork_id, dir='none', len=0.3, color=color, style='bold')
                for target in filtered:
                    make_edge(source, target, kind, source_id=fork_id, len=1.3, color=color)
            else:
                make_edge(source, filtered[0], kind, color=color)

    write('{}', '''\
digraph "aptorphan" {
    ratio="auto";
    node [fontcolor="black",shape="box",style="filled",fixedsize=false,width=0.5,height=0.2,fontname="Courier",fontsize=10.0,margin="0.1,0.02"];
    edge [fontsize=8.0,len=1.5];
''')

    make_raw_node(':config', label='config', color='#b3e2cd', shape='ellipse', root='true', fontname='Times-Italic')

    for version in anchors:
        make_raw_node(version.id(), label=version.display_name(), color='#e6f5c9', shape='ellipse')
        if version in explicits:
            for filename in explicits[version]:
                make_raw_edge(':config', version.id(), color='#b3e2cd', label=filename)
        if version in implicits:
            make_raw_edge(':config', version.id(), color='#b3e2cd', label=implicits[version])
        make_edges(version, Global.depends, '#e6f5c9')

    for version in spurious:
        make_raw_node(version.id(), label=version.display_name(), color='#f4cae4')
        make_edges(version, None, '#f4cae4')

    for version in missing:
        color = '#fdcdac' if version in candidates else '#f2f2f2'
        make_raw_node(version.id(), label=version.display_name(), color=color)
        make_edges(version, None, color)

    write('{}\n', '}')
