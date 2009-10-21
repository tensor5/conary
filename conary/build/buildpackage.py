#
# Copyright (c) 2004-2009 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.rpath.com/permanent/licenses/CPL-1.0.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#

"""
Contains classes used during the build process to collect files
into BuildComponents.  These BuildComponents are used to create Packages
and create changesets from the files created during the build process
"""

import os
import time

from conary import files
from conary.lib import sha1helper
from conary.build import use
from conary.deps import deps

def BuildDeviceFile(devtype, major, minor, owner, group, perms):
    if devtype == "b":
	f = files.BlockDevice(None)
    elif devtype == "c":
	f = files.CharacterDevice(None)
    else:
	raise AssertionError

    f.devt.major.set(major)
    f.devt.minor.set(minor)
    f.inode.owner.set(owner)
    f.inode.group.set(group)
    f.inode.perms.set(perms)
    f.inode.mtime.set(int(time.time()))
    f.flags.set(0)

    return f

def _getUseFlavor(recipe):
    """
    Returns a deps.Flavor instance that represents the Use flags
    that have been used.
    """
    if 'abstractBaseClass' in recipe.__class__.__dict__ \
            and recipe.abstractBaseClass:
        # abstract base classes shouldn't be flavored
        return deps.Flavor()
    f = use.createFlavor(recipe.name, use.Use._iterUsed(),
                         recipe.Flags._iterUsed(),
                         use.Arch._iterUsed(), 
                         targetDep=recipe.isCrossCompileTool())
    if recipe.isCrossCompileTool():
        # there's no guarantee that a cross compiler tool will mention
        # anything in its flavor to automatically add the target flavor.
        # We have to do it manually.
        f.union(recipe.targetFlavor)
    return f

class BuildComponent(dict):

    def addFile(self, path, realPath):
        """
        Add a file to the build component

        @param path: the destination of the file in the component
        @param realPath: the location of the actual file on the filesystem,
        used to obtain the contents of the file when creating a changeset
        to commit to the repository
        """
        # skip uid/gid lookups because packagepolicy will change the
        # ownerships according to Ownership settings anyway
	(f, linkCount, inode) = files.FileFromFilesystem(realPath, None, 
                                        inodeInfo = True, assumeRoot = True)
	f.inode.perms.set(f.inode.perms() & 01777)
	self[path] = (realPath, f)
        if (f.inode.perms() & 0400) != 0400:
            # we can safely change the permissions now, the original
            # permissions have been recorded
            os.chmod(realPath, f.inode.perms() | 0400)

        if linkCount > 1:
            if f.hasContents:
                l = self.linkGroups.get(inode, [])
                l.append(path)
                self.linkGroups[inode] = l
                # add to list to check for config files later
                self.hardlinkMap[path] = l
            else:
                if not isinstance(f, files.Directory):
                    # no hardlinks allowed for special files other than dirs
                    self.badhardlinks.append(path)
        return f

    def addDevice(self, path, devtype, major, minor,
                  owner='root', group='root', perms=0660):
        """
        Add a device node to the build component

        @param path: the destination of the device node in the component
        """
        f = BuildDeviceFile(devtype, major, minor, owner, group, perms)
	self[path] = (None, f)
        return f

    def getFile(self, path):
        return self[path][1]

    def getRealPath(self, path):
        return self[path][0]

    def getName(self):
        """
        Return the name of the BuildComponent

        @returns: name of the BuildComponent
        @rtype: str
        """
	return self.name

    def getUserMap(self):
        """
        Dict mapping user names to tuples of C{(preferred_uid, groupname,
        preferred_groupid, homedir, comment, shell)}
        """
        return self.recipe.usermap

    def getUserGroupMap(self):
        """
        Reverse map from group name to user name for groups created as part
        of a user definition.
        """
        return self.recipe.usergrpmap

    def getGroupMap(self):
        """
        Dict mapping group names to preferred_groupid
        """
        return self.recipe.groupmap

    def getSuppGroupMap(self):
        """
        Dict mapping user names to C{(group, preferred_groupid)} tuples
        """
        return self.recipe.suppmap

    def __init__(self, name, recipe):
        self.name = name

        self.requires = deps.DependencySet()
        self.provides = deps.DependencySet()
        self.provides.addDep(deps.TroveDependencies, deps.Dependency(name))
        self.flavor = _getUseFlavor(recipe)
        self.linkGroups = {}
        self.requiresMap = {}
        self.providesMap = {}
        self.hardlinkMap = {}
        self.badhardlinks = []
        self.recipe = recipe
	dict.__init__(self)


class AutoBuildPackage:
    """
    AutoBuildPackage creates a set of BuildComponent instances and
    provides facilities for automatically populating them with files
    according to Filters.
    """
    def __init__(self, pkgFilters, compFilters, recipe):
        """
	@param pkgFilters: Filters used to add files to main packages
	@type pkgFilters: sequence of Filter instances
	@param compFilters: Filters used to add files to components
	@type compFilters: sequence of Filter instances
	"""
	self.pkgFilters = pkgFilters
        self.compFilters = compFilters
        self.recipe = recipe

        # dictionary of all the components
        self.components = {}
        # reverse map from the package:component combination to
        # the correct build package
	self.packageMap = {}
	for main in self.pkgFilters:
	    for comp in self.compFilters:
		name = self._getname(main.name, comp.name)
		if name not in self.components:
		    self.components[name] = BuildComponent(name, recipe)
		if main not in self.packageMap:
		    self.packageMap[main] = {}
		self.packageMap[main][comp] = self.components[name]
	# dictionary from pathnames to fileobjects
	self.pathMap = {}
	# dictionary from pathnames to packages
	self.componentMap = {}

    def _getname(self, pkgname, compname):
        return ':'.join((pkgname, compname))

    def findComponent(self, path, mode=None):
        """
	Return the BuildComponent that matches the path.
	"""
        if path in self.componentMap:
            return self.componentMap[path]
	for main in self.pkgFilters:
	    if main.match(path, mode=mode):
		for comp in self.compFilters:
		    if comp.match(path, mode=mode):
			self.componentMap[path] = self.packageMap[main][comp]
                        return self.componentMap[path]
        return None
    
    def updateFileContents(self, path, realPath):
        """
        Update contents information, including sha1 and contents
        """
        f = self.pathMap[path]
        assert(f.hasContents and isinstance(f, files.RegularFile))
        sha1 = sha1helper.sha1FileBin(realPath)
        size = os.lstat(realPath).st_size
        f.contents.size.set(size)
        f.contents.sha1.set(sha1)

    def addFile(self, path, realPath):
        """
        Add a path to the correct BuildComponent instance by matching
        the file name against the package and component filters

        @param path: path to add to the BuildComponent
        @type path: str
        @rtype: None
        """
        pkg = self.findComponent(path)
        self.pathMap[path] = pkg.addFile(path, realPath)

    def delFile(self, path):
        """
	Remove a file from the package and from the caches.

        @param path: path to remove from the BuildComponent
        @type path: str
        @rtype: None
        """
	del self.componentMap[path][path]
	del self.componentMap[path]
	del self.pathMap[path]

    def addDevice(self, path, devtype, major, minor,
                  owner='root', group='root', perms=0660):
        """
        Add a device to the correct BuildComponent instance by matching
        the file name against the package and component filters
        """
        pkg = self.findComponent(path, mode=perms)
        f = pkg.addDevice(path, devtype, major, minor, owner, group, perms)
	self.pathMap[path] = f
	self.componentMap[path] = pkg

    def getComponents(self):
        """
        Examine the BuildComponent instances that have been created and
        return a list that includes only those which have files
        
        @return: list of BuildComponent instances
        @rtype: list
        """
        l = []
        for componentName in self.components.keys():
            if (self.components[componentName].keys() or
                self.recipe._hasCapsulePackage(componentName)):
                # there are files or there is a capsule
                l.append(self.components[componentName])
        return l
