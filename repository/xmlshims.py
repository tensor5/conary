#
# Copyright (c) 2004 Specifix, Inc.
# All rights reserved
#

import deps.deps
import versions
import files
import base64

class NetworkConvertors:

    def freezeVersion(self, v):
	return v.freeze()

    def thawVersion(self, v):
	return versions.ThawVersion(v)

    def fromVersion(self, v):
	return v.asString()

    def toVersion(self, v):
	return versions.VersionFromString(v)

    def fromBranch(self, b):
	return b.asString()

    def toBranch(self, b):
	return versions.VersionFromString(b)

    def toFlavor(self, f):
	if f == 0 or f == "none" or f is None:
	    return None

	return deps.deps.ThawDependencySet(f)

    def fromFlavor(self, f):
	if f is None:
	    return 0

	return f.freeze()

    def toFile(self, f):
        fileId = f[:40]
        return files.ThawFile(base64.decodestring(f[40:]), fileId)

    def fromFile(self, f):
        s = base64.encodestring(f.freeze())
        return f.id() + s

    def fromLabel(self, l):
	return l.asString()

    def toLabel(self, l):
	return versions.BranchName(l)
