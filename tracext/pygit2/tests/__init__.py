# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2014 Jun Omae <jun66j5@gmail.com>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

import unittest

from tracext.pygit2.tests import git_fs


def suite():
    suite = unittest.TestSuite()
    suite.addTest(git_fs.suite())
    return suite


if __name__ == '__main__':
    unittest.main(defaultTest='suite')
