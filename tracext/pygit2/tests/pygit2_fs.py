# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2014 Jun Omae <jun66j5@gmail.com>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

import os.path
import shutil
import sys
import tempfile
import unittest
from cStringIO import StringIO
from datetime import datetime
from subprocess import Popen, PIPE
try:
    import pygit2
except ImportError:
    pygit2 = None

from trac.core import TracError
from trac.test import EnvironmentStub, locate
from trac.util.compat import close_fds
from trac.util.datefmt import parse_date, utc
from trac.versioncontrol import DbRepositoryProvider, RepositoryManager
from trac.versioncontrol.api import (
    Changeset, Node, NoSuchChangeset, NoSuchNode,
)
from tracext.pygit2 import pygit2_fs


REPOS_NAME = 'test.git'
REPOS_URL = 'http://example.org/git/test.git'
HEAD_REV = u'97553583461dd682de4db752f1e102fb19b019d8'
HEAD_ABBREV = u'9755358'
ROOT_REV = u'fc398de9939a675d6001f204c099215337d4eb24'
ROOT_ABBREV = u'fc398de'

dumpfile_path = os.path.join(os.path.dirname(__file__), 'gitrepos.dump')
repos_path = None
git_bin = None


def spawn(*args, **kwargs):
    kw = {'stdin': PIPE, 'stdout': PIPE, 'stderr': PIPE,
          'close_fds': close_fds}
    kw.update(kwargs)
    return Popen(args, **kw)


def create_repository(path, use_dump=True, data=None):
    pygit2.init_repository(path, True)
    if data is None and use_dump:
        f = open(dumpfile_path, 'rb')
        try:
            data = f.read()
        finally:
            f.close()
    if data is not None:
        proc = spawn(git_bin, '--git-dir=' + path, 'fast-import')
        stdout, stderr = proc.communicate(input=data)
        assert proc.returncode == 0, stderr


def setup_repository(env, path, reponame=REPOS_NAME, sync=True):
    provider = DbRepositoryProvider(env)
    provider.add_repository(reponame, path, 'pygit2')
    provider.modify_repository(reponame, {'url': REPOS_URL})
    repos = env.get_repository(reponame)
    if sync:
        repos.sync()
    return repos


def rmtree(path):
    import errno

    def onerror(function, path, excinfo):
        # `os.remove` fails for a readonly file on Windows.
        # Then, it attempts to be writable and remove.
        if function != os.remove:
            raise
        e = excinfo[1]
        if isinstance(e, OSError) and e.errno == errno.EACCES:
            mode = os.stat(path).st_mode
            os.chmod(path, mode | 0666)
            function(path)
        raise
    if os.name == 'nt':
        # Git repository for tests has unicode characters
        # in the path and branch names
        path = unicode(path, 'utf-8')
    shutil.rmtree(path, onerror=onerror)


class GitRepositoryTestSuite(unittest.TestSuite):

    use_dump = True

    def run(self, result):
        try:
            self.setUp()
            unittest.TestSuite.run(self, result)
        finally:
            self.tearDown()
        return result

    def setUp(self):
        create_repository(repos_path, self.use_dump)

    def tearDown(self):
        if os.path.isdir(repos_path):
            rmtree(repos_path)


class EmptyGitRepositoryTestSuite(GitRepositoryTestSuite):

    use_dump = False


class GitTestCaseSetup(object):

    def setUp(self):
        self.env = EnvironmentStub(enable=['trac.*', 'tracext.pygit2.*'])
        self.env.config.set('git', 'cached_repository',
                            '01'[self.cached_repository])
        self.repos = repos = setup_repository(self.env, repos_path)
        if self.cached_repository:
            repos = repos.repos
        self.git_repos = repos.git_repos

    def tearDown(self):
        self.git_repos = None
        self.repos.close()
        self.repos = None
        RepositoryManager(self.env).reload_repositories()
        if self.env.dburi == 'sqlite::memory:':
            # workaround to avoid "OperationalError: no such table: repository"
            # on Trac 1.0+ with sqlite::memory:
            import gc
            gc.collect()
        self.env.reset_db()


class EmptyTestCase(object):

    def test_empty(self):
        if hasattr(pygit2.Repository, 'is_empty'):
            self.assertEquals(True, self.git_repos.is_empty)
        self.assertRaises(pygit2.GitError, lambda: self.git_repos.head)

    def test_get_quickjump_entries(self):
        entries = list(self.repos.get_quickjump_entries(None))
        self.assertEquals([], entries)

    def test_get_changeset(self):
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset, None)
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset, '')
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset, u'')
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset, 42)
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset,
                          ROOT_ABBREV)
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset,
                          ROOT_REV)

    def test_get_changesets(self):
        start = datetime(2001, 1, 1, tzinfo=utc)
        stop = datetime(2014, 1, 1, tzinfo=utc)
        changesets = self.repos.get_changesets(start, stop)
        self.assertRaises(StopIteration, changesets.next)

    def test_has_node(self):
        self.assertEquals(False, self.repos.has_node('/', '1' * 40))
        self.assertEquals(True, self.repos.has_node('/'))
        self.assertEquals(False, self.repos.has_node('/', ROOT_ABBREV))

    def test_get_node(self):
        node = self.repos.get_node('/')
        self.assertEquals('', node.path)
        self.assertEquals('', node.created_path)
        self.assertEquals(None, node.rev)
        self.assertEquals(None, node.created_rev)
        self.assertEquals([], list(node.get_entries()))
        self.assertEquals([], list(node.get_history()))
        self.assertRaises(NoSuchChangeset, self.repos.get_node, '/',
                          ROOT_ABBREV)
        self.assertRaises(NoSuchNode, self.repos.get_node, '/path')
        self.assertRaises(NoSuchChangeset, self.repos.get_node, '/path',
                          ROOT_ABBREV)

    def test_oldest_rev(self):
        self.assertEquals(None, self.repos.oldest_rev)

    def test_youngest_rev(self):
        self.assertEquals(None, self.repos.youngest_rev)

    def test_previous_rev_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.previous_rev, None)
        self.assertRaises(NoSuchChangeset, self.repos.previous_rev, '')

    def test_next_rev_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.next_rev, None)
        self.assertRaises(NoSuchChangeset, self.repos.next_rev, '')

    def test_parent_revs_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.parent_revs, None)
        self.assertRaises(NoSuchChangeset, self.repos.parent_revs, '')

    def test_child_revs_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.child_revs, None)
        self.assertRaises(NoSuchChangeset, self.repos.child_revs, '')

    def test_rev_older_than_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.rev_older_than,
                          None, None)
        self.assertRaises(NoSuchChangeset, self.repos.rev_older_than, '', '')

    def test_get_path_history(self):
        self.assertRaises(TracError, self.repos.get_path_history, '/')

    def test_normalize_rev(self):
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, None)
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, ROOT_REV)
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, '1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, '0' * 40)

    def test_get_changes(self):
        self.assertRaises(NoSuchChangeset, self.repos.get_changes,
                          '/', ROOT_ABBREV, '/', '0ee9cfd')


class NormalTestCase(object):

    def test_not_empty(self):
        if hasattr(pygit2.Repository, 'is_empty'):
            self.assertEquals(False, self.git_repos.is_empty)
        self.assertEquals(pygit2.GIT_REF_OID, self.git_repos.head.type)

    def test_linear_changesets(self):
        self.assertEquals(False, self.repos.has_linear_changesets)

    def test_clear(self):
        pass  # TODO: GitRepository.clear(self, youngest_rev=None)

    def test_get_quickjump_entries(self):
        entries = self.repos.get_quickjump_entries(None)
        self.assertEquals(('branches', u'develöp', '/', HEAD_REV),
                          entries.next())
        self.assertEquals(('branches', u'master', '/', HEAD_REV),
                          entries.next())
        self.assertEquals(('branches', u'stâble', '/', HEAD_REV),
                          entries.next())
        self.assertEquals(('tags', u'ver0.1', '/', ROOT_REV), entries.next())
        self.assertEquals(('tags', u'vér0.1', '/', ROOT_REV), entries.next())
        self.assertRaises(StopIteration, entries.next)

    def test_get_path_url(self):
        self.assertEquals(REPOS_URL, self.repos.get_path_url('/', None))
        self.assertEquals(REPOS_URL, self.repos.get_path_url('/', ROOT_ABBREV))
        self.assertEquals(REPOS_URL, self.repos.get_path_url('/.gitignore',
                                                             None))
        self.assertEquals(REPOS_URL, self.repos.get_path_url('/.gitignore',
                                                             ROOT_ABBREV))

    def test_get_path_url_not_specified(self):
        provider = DbRepositoryProvider(self.env)
        reponame = REPOS_NAME + '.alternative'
        provider.add_repository(reponame, repos_path, 'pygit2')
        repos = self.env.get_repository(reponame)
        self.assertEquals(None, repos.get_path_url('/', None))

    def test_get_changeset_nonexistent(self):
        self.assertEquals(HEAD_REV, self.repos.get_changeset(None).rev)
        self.assertEquals(HEAD_REV, self.repos.get_changeset('').rev)
        self.assertEquals(HEAD_REV, self.repos.get_changeset(u'').rev)

        self.assertRaises(NoSuchChangeset, self.repos.get_changeset, u'1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset, 42)
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset, 42L)
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset, 4.2)

    def test_changeset_add(self):
        cset = self.repos.get_changeset(ROOT_ABBREV)
        self.assert_(isinstance(cset, Changeset), repr(cset))
        self.assertEquals(ROOT_REV, cset.rev)
        if self.cached_repository:
            self.assertEquals(parse_date('2013-02-14T23:01:25+09:00'),
                              cset.date)
        else:
            self.assertEquals('2013-02-14T23:01:25+09:00',
                              cset.date.isoformat())
        self.assertEquals(u'Add some files\n', cset.message)
        self.assertEquals(u'Joé <joe@example.com>', cset.author)

        changes = cset.get_changes()
        self.assertEquals((u'.gitignore', Node.FILE, Changeset.ADD, None,
                           None),
                          changes.next())
        self.assertEquals((u'dir/sample.txt', Node.FILE, Changeset.ADD, None,
                           None),
                          changes.next())
        self.assertEquals((u'dir/tété.txt', Node.FILE, Changeset.ADD, None,
                           None),
                          changes.next())
        self.assertEquals((u'root-tété.txt', Node.FILE, Changeset.ADD, None,
                           None),
                          changes.next())
        self.assertEquals((u'āāā/file.txt', Node.FILE, Changeset.ADD, None,
                           None),
                          changes.next())
        self.assertRaises(StopIteration, changes.next)

    def test_changeset_others(self):
        cset = self.repos.get_changeset('0ee9cfd')
        self.assert_(isinstance(cset, Changeset), repr(cset))
        self.assertEquals(u'0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                          cset.rev)
        if self.cached_repository:
            self.assertEquals(parse_date('2013-02-15T01:02:07+09:00'),
                              cset.date)
        else:
            self.assertEquals('2013-02-15T01:02:07+09:00',
                              cset.date.isoformat())
        self.assertEquals(u'delete, modify, rename, copy\n', cset.message)
        self.assertEquals(u'Joé <joe@example.com>', cset.author)

        changes = cset.get_changes()
        self.assertEquals((u'dir/tété.txt', Node.FILE, Changeset.DELETE,
                           u'dir/tété.txt', ROOT_REV),
                          changes.next())
        self.assertEquals((u'dir2/simple-another.txt', Node.FILE,
                           Changeset.ADD, None, None),
                          changes.next())
        self.assertEquals((u'dir2/simple.txt', Node.FILE, Changeset.ADD,
                           None, None),
                          changes.next())
        # Copy root-sample.txt <- dir/sample.txt
        self.assertEquals((u'root-sample.txt', Node.FILE, Changeset.ADD,
                           None, None),
                          changes.next())
        self.assertEquals((u'root-tété.txt', Node.FILE, Changeset.EDIT,
                           u'root-tété.txt', ROOT_REV),
                          changes.next())
        # Rename āāā-file.txt <- āāā/file.txt
        self.assertEquals((u'āāā-file.txt', Node.FILE, Changeset.MOVE,
                           u'āāā/file.txt', ROOT_REV),
                          changes.next())
        self.assertRaises(StopIteration, changes.next)

    def test_changeset_get_branches(self):
        self.assertEquals(
            [(u'develöp', False), ('master', False), (u'stâble', False)],
            self.repos.get_changeset(ROOT_ABBREV).get_branches())
        self.assertEquals(
            [(u'develöp', True), ('master', True), (u'stâble', True)],
            self.repos.get_changeset(HEAD_REV[:7]).get_branches())

    def test_changeset_get_tags(self):
        self.assertEquals([u'ver0.1', u'vér0.1'],
                          self.repos.get_changeset(ROOT_ABBREV).get_tags())
        self.assertEquals([], self.repos.get_changeset('0ee9cfd').get_tags())

    def test_get_changeset_uid(self):
        self.assertEquals(ROOT_REV, self.repos.get_changeset_uid(ROOT_REV))

    def test_get_changesets(self):
        changesets = self.repos.get_changesets(
            datetime(2013, 2, 13, 15, 0, 0, tzinfo=utc),
            datetime(2013, 2, 14, 15, 0, 0, tzinfo=utc))
        self.assertEquals(ROOT_REV, changesets.next().rev)
        self.assertRaises(StopIteration, changesets.next)

        changesets = self.repos.get_changesets(
            datetime(2013, 2, 14, 14, 0, 0, tzinfo=utc),
            datetime(2013, 2, 14, 17, 0, 0, tzinfo=utc))
        self.assertEquals('0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                          changesets.next().rev)
        self.assertEquals(ROOT_REV, changesets.next().rev)
        self.assertRaises(StopIteration, changesets.next)

    def test_has_node(self):
        self.assertEquals(False, self.repos.has_node('/', '1' * 40))
        self.assertEquals(True, self.repos.has_node('/'))
        self.assertEquals(True, self.repos.has_node('/', '0ee9cfd'))
        self.assertEquals(True, self.repos.has_node('/'))
        self.assertEquals(True, self.repos.has_node('/.gitignore',
                                                    ROOT_ABBREV))

    def test_get_node(self):
        node = self.repos.get_node(u'/')
        self.assertEquals(HEAD_REV, node.rev)
        self.assertEquals('', node.path)

    def test_get_node_invalid_rev(self):
        self.assertRaises(NoSuchChangeset, self.repos.get_node, u'/', '1' * 40)

    def test_get_node_nonexistent(self):
        self.assertRaises(NoSuchNode, self.repos.get_node, u'/āāā/file.txt',
                          '0ee9cfd')

    def test_get_node_directory(self):
        node = self.repos.get_node(u'/dir', ROOT_ABBREV)
        self.assertEquals(u'dir', node.name)
        self.assertEquals(u'dir', node.path)
        self.assertEquals(Node.DIRECTORY, node.kind)
        self.assertEquals(ROOT_REV, node.rev)
        self.assertEquals(ROOT_REV, node.created_rev)
        self.assertEquals(None, node.content_type)
        self.assertEquals(None, node.content_length)
        self.assertEquals(None, node.get_content())
        self.assertEquals(None, node.last_modified)
        self.assertEquals({'mode': '040000'}, node.get_properties())
        entries = node.get_entries()
        self.assertEquals(u'dir/sample.txt', entries.next().path)
        self.assertEquals(u'dir/tété.txt', entries.next().path)
        self.assertRaises(StopIteration, entries.next)

        node = self.repos.get_node(u'/', '0ee9cfd')
        self.assertEquals(Node.DIRECTORY, node.kind)
        self.assertEquals({}, node.get_properties())
        entries = node.get_entries()
        self.assertEquals(u'.gitignore', entries.next().path)
        self.assertEquals(u'dir', entries.next().path)
        self.assertEquals(u'dir2', entries.next().path)
        self.assertEquals(u'root-sample.txt', entries.next().path)
        self.assertEquals(u'root-tété.txt', entries.next().path)
        self.assertEquals(u'āāā-file.txt', entries.next().path)
        self.assertRaises(StopIteration, entries.next)

    def test_get_node_file(self):
        node = self.repos.get_node(u'/dir/sample.txt', '0ee9cfd')
        self.assertEquals(u'sample.txt', node.name)
        self.assertEquals(u'dir/sample.txt', node.path)
        self.assertEquals(Node.FILE, node.kind)
        self.assertEquals(u'0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                          node.rev)
        self.assertEquals(ROOT_REV, node.created_rev)
        self.assertRaises(StopIteration, node.get_entries().next)
        self.assertEquals('', node.content_type)
        self.assertEquals(465, node.content_length)
        content = node.get_content().read()
        self.assertEquals(str, type(content))
        self.assertEquals(465, len(content))
        if self.cached_repository:
            self.assertEquals(parse_date('2013-02-14T23:01:25+09:00'),
                              node.last_modified)
        else:
            self.assertEquals('2013-02-14T23:01:25+09:00',
                              node.last_modified.isoformat())
        self.assertEquals({'mode': '100644'}, node.get_properties())

        node = self.repos.get_node(u'/āāā-file.txt', '0ee9cfd')
        self.assertEquals(u'āāā-file.txt', node.name)
        self.assertEquals(u'āāā-file.txt', node.path)
        self.assertEquals(Node.FILE, node.kind)
        self.assertEquals(u'0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                          node.rev)
        self.assertEquals(u'0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                          node.created_rev)
        self.assertRaises(StopIteration, node.get_entries().next)
        self.assertEquals('', node.content_type)
        self.assertEquals(37, node.content_length)
        content = node.get_content().read()
        self.assertEquals(str, type(content))
        self.assertEquals(37, len(content))
        self.assertEquals('The directory has unicode characters\n', content)
        if self.cached_repository:
            self.assertEquals(parse_date('2013-02-15T01:02:07+09:00'),
                              node.last_modified)
        else:
            self.assertEquals('2013-02-15T01:02:07+09:00',
                              node.last_modified.isoformat())
        self.assertEquals({'mode': '100644'}, node.get_properties())

    def test_node_get_history(self):
        node = self.repos.get_node(u'/root-tété.txt')
        history = node.get_history()
        self.assertEquals((u'root-tété.txt',
                           u'0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                           'edit'),
                          history.next())
        self.assertEquals((u'root-tété.txt', ROOT_REV, 'add'), history.next())
        self.assertRaises(StopIteration, history.next)

        node = self.repos.get_node(u'/root-tété.txt')
        history = node.get_history(1)
        self.assertEquals((u'root-tété.txt',
                           u'0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                           'edit'),
                          history.next())
        self.assertRaises(StopIteration, history.next)

        node = self.repos.get_node(u'/root-tété.txt', ROOT_ABBREV)
        history = node.get_history()
        self.assertEquals((u'root-tété.txt', ROOT_REV, 'add'), history.next())
        self.assertRaises(StopIteration, history.next)

    def test_node_get_history_root_dir(self):
        expected = [('', HEAD_REV,                                   'edit'),
                    ('', '5fa8e424840c6c4dd331343550d870e6faafadf5', 'edit'),
                    ('', '0ee9cfd6538b7b994b94a45ed173d9d45272b0c5', 'edit'),
                    ('', ROOT_REV,                                   'add')]
        node = self.repos.get_node(u'/')
        self.assertEquals(expected, list(node.get_history()))
        node = self.repos.get_node(u'/', HEAD_ABBREV)
        self.assertEquals(expected, list(node.get_history()))
        node = self.repos.get_node(u'/', HEAD_ABBREV)
        self.assertEquals(expected[:1], list(node.get_history(1)))
        self.assertEquals(expected[:3], list(node.get_history(3)))
        self.assertEquals(expected, list(node.get_history(4)))
        self.assertEquals(expected, list(node.get_history(5)))

        expected = [('', ROOT_REV, 'add')]
        node = self.repos.get_node(u'/', ROOT_REV)
        self.assertEquals(expected, list(node.get_history()))
        node = self.repos.get_node(u'/', ROOT_ABBREV)
        self.assertEquals(expected, list(node.get_history()))
        self.assertEquals(expected, list(node.get_history(1)))
        self.assertEquals(expected, list(node.get_history(2)))

    def test_get_node_submodule(self):
        node = self.repos.get_node('/')
        entries = dict((node.path, node) for node in node.get_entries())
        self.assertTrue(u'submod' in entries)

        node = entries.get(u'submod')
        self.assertNotEquals(None, node)
        self.assertEquals(HEAD_REV, node.rev)
        self.assertTrue(node.isdir)
        self.assertFalse(node.isfile)
        self.assertEquals({'mode': '160000'}, node.get_properties())
        self.assertEquals([], list(node.get_entries()))

        node = self.repos.get_node('/submod')
        self.assertNotEquals(None, node)
        self.assertTrue(node.isdir)
        self.assertFalse(node.isfile)
        self.assertEquals({'mode': '160000'}, node.get_properties())
        self.assertEquals([], list(node.get_entries()))

    def test_oldest_rev(self):
        self.assertEquals(ROOT_REV, self.repos.oldest_rev)

    def test_youngest_rev(self):
        self.assertEquals(HEAD_REV, self.repos.youngest_rev)

    # TODO: GitRepository.previous_rev(self, rev, path=''):

    def test_previous_rev_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.previous_rev, '1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.previous_rev, '1' * 40,
                          '/.gitignore')

    # TODO: GitRepository.next_rev(self, rev, path=''):

    def test_next_rev_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.next_rev, '1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.next_rev, '1' * 40,
                          '/.gitignore')

    # TODO: GitRepository.parent_revs(self, rev):

    def test_parent_revs_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.parent_revs, '1' * 40)

    # TODO: GitRepository.child_revs(self, rev):

    def test_child_revs_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.child_revs, '1' * 40)

    def test_rev_older_than(self):
        self.assertEquals(True, self.repos.rev_older_than(ROOT_REV, HEAD_REV))
        self.assertEquals(True, self.repos.rev_older_than(ROOT_ABBREV,
                                                          HEAD_ABBREV))
        self.assertEquals(False, self.repos.rev_older_than(HEAD_REV, ROOT_REV))
        self.assertEquals(False, self.repos.rev_older_than(HEAD_ABBREV,
                                                           ROOT_ABBREV))
        self.assertEquals(False, self.repos.rev_older_than(HEAD_REV, HEAD_REV))
        self.assertEquals(False, self.repos.rev_older_than(HEAD_ABBREV,
                                                           HEAD_ABBREV))
        self.assertEquals(False, self.repos.rev_older_than(ROOT_REV, ROOT_REV))
        self.assertEquals(False, self.repos.rev_older_than(ROOT_ABBREV,
                                                           ROOT_ABBREV))

    def test_rev_older_than_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.rev_older_than,
                          '1' * 40, '1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.rev_older_than, ROOT_REV,
                          '1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.rev_older_than, '1' * 40,
                          ROOT_REV)

    def test_normalize_path(self):
        self.assertEquals('', self.repos.normalize_path('/'))
        self.assertEquals(u'git/git_fs.py',
                          self.repos.normalize_path('git/git_fs.py'))
        self.assertEquals(u'pæth/to', self.repos.normalize_path('pæth/to'))
        self.assertEquals(u'pæth/to', self.repos.normalize_path('pæth/to/'))
        self.assertEquals(u'pæth/to', self.repos.normalize_path('/pæth/to'))
        self.assertEquals(u'pæth/to', self.repos.normalize_path('/pæth/to/'))

    def test_normalize_rev_empty(self):
        self.assertEquals(HEAD_REV, self.repos.normalize_rev(None))
        self.assertEquals(HEAD_REV, self.repos.normalize_rev(''))
        self.assertEquals(HEAD_REV, self.repos.normalize_rev(u''))

    def test_normalize_rev(self):
        rev = str(ROOT_REV)
        urev = unicode(ROOT_REV)
        self.assertEquals(unicode, type(self.repos.normalize_rev(rev[:7])))
        self.assertEquals(unicode, type(self.repos.normalize_rev(urev[:7])))
        self.assertEquals(urev, self.repos.normalize_rev(rev[:7]))
        self.assertEquals(urev, self.repos.normalize_rev(urev[:7]))
        self.assertEquals(urev, self.repos.normalize_rev(rev[:20]))
        self.assertEquals(urev, self.repos.normalize_rev(urev[:20]))
        self.assertEquals(urev, self.repos.normalize_rev(rev))
        self.assertEquals(urev, self.repos.normalize_rev(urev))

    def test_normalize_rev_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, u'1' * 40)

    def test_normalize_rev_non_string(self):
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, True)
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, False)
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, 42)
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, 42L)
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, 4.2)

    def test_short_rev(self):
        rev = str(ROOT_REV)
        urev = unicode(ROOT_REV)
        self.assertEquals(unicode, type(self.repos.short_rev(rev)))
        self.assertEquals(ROOT_ABBREV, self.repos.short_rev(rev))
        self.assertEquals(ROOT_ABBREV, self.repos.short_rev(rev[:7]))
        self.assertEquals(unicode, type(self.repos.short_rev(urev)))
        self.assertEquals(ROOT_ABBREV, self.repos.short_rev(urev))
        self.assertEquals(ROOT_ABBREV, self.repos.short_rev(urev[:7]))

    def test_short_rev_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, u'1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, True)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, False)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, 42)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, 42L)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, 4.2)

    def test_display_rev(self):
        rev = str(ROOT_REV)
        urev = unicode(ROOT_REV)
        self.assertEquals(unicode, type(self.repos.display_rev(rev)))
        self.assertEquals(ROOT_ABBREV, self.repos.display_rev(rev))
        self.assertEquals(ROOT_ABBREV, self.repos.display_rev(rev[:7]))
        self.assertEquals(unicode, type(self.repos.display_rev(urev)))
        self.assertEquals(ROOT_ABBREV, self.repos.display_rev(urev))
        self.assertEquals(ROOT_ABBREV, self.repos.display_rev(urev[:7]))

    def _cmp_change(self, expected, change):
        self.assertEquals(expected, (change[0] and change[0].path,
                                     change[1] and change[1].path,
                                     change[2], change[3]))

    def test_get_changes_different_revs(self):
        changes = self.repos.get_changes('/', ROOT_ABBREV, '/', '0ee9cfd')
        self._cmp_change((u'dir/tété.txt', None, Node.FILE, Changeset.DELETE),
                         changes.next())
        self._cmp_change((None, u'dir2/simple-another.txt', Node.FILE,
                          Changeset.ADD),
                         changes.next())
        self._cmp_change((None, u'dir2/simple.txt', Node.FILE, Changeset.ADD),
                         changes.next())
        # Copy root-sample.txt <- dir/sample.txt
        self._cmp_change((None, u'root-sample.txt', Node.FILE, Changeset.ADD),
                         changes.next())
        self._cmp_change((u'root-tété.txt', u'root-tété.txt', Node.FILE,
                          Changeset.EDIT),
                          changes.next())
        # Rename āāā-file.txt <- āāā/file.txt
        self._cmp_change((u'āāā/file.txt', u'āāā-file.txt', Node.FILE,
                          Changeset.MOVE),
                         changes.next())
        self.assertRaises(StopIteration, changes.next)

    def test_get_annotations_with_head(self):
        expect = ['97553583461dd682de4db752f1e102fb19b019d8',
                  'fc398de9939a675d6001f204c099215337d4eb24',
                  'fc398de9939a675d6001f204c099215337d4eb24',
                  'fc398de9939a675d6001f204c099215337d4eb24',
                  'fc398de9939a675d6001f204c099215337d4eb24',
                  'fc398de9939a675d6001f204c099215337d4eb24',
                  'fc398de9939a675d6001f204c099215337d4eb24',
                  '97553583461dd682de4db752f1e102fb19b019d8',
                  '97553583461dd682de4db752f1e102fb19b019d8',
                  '97553583461dd682de4db752f1e102fb19b019d8',
                  'fc398de9939a675d6001f204c099215337d4eb24',
                  'fc398de9939a675d6001f204c099215337d4eb24',
                  '97553583461dd682de4db752f1e102fb19b019d8']
        node = self.repos.get_node('/dir/sample.txt')
        self.assertEquals(expect, node.get_annotations())
        node = self.repos.get_node('/dir/sample.txt', HEAD_REV)
        self.assertEquals(expect, node.get_annotations())
        node = self.repos.get_node('/dir/sample.txt', HEAD_ABBREV)
        self.assertEquals(expect, node.get_annotations())

    def test_get_annotations_with_revision(self):
        expected = ['fc398de9939a675d6001f204c099215337d4eb24'] * 9
        node = self.repos.get_node('/dir/sample.txt', '0ee9cfd')
        self.assertEquals(expected, node.get_annotations())
        node = self.repos.get_node('/dir/sample.txt',
                                   '0ee9cfd6538b7b994b94a45ed173d9d45272b0c5')
        self.assertEquals(expected, node.get_annotations())

    def test_get_annotations_unicode(self):
        node = self.repos.get_node(u'/root-tété.txt', ROOT_ABBREV)
        self.assertEquals(['fc398de9939a675d6001f204c099215337d4eb24'],
                          node.get_annotations())
        node = self.repos.get_node(u'/root-tété.txt',
                                   'fc398de9939a675d6001f204c099215337d4eb24')
        self.assertEquals(['fc398de9939a675d6001f204c099215337d4eb24'],
                          node.get_annotations())

    def test_sync_too_many_merges(self):
        data = self._generate_data_many_merges(100)
        repos_path = tempfile.mkdtemp(prefix='trac-gitrepos-')
        try:
            create_repository(repos_path, data=data)
            repos = setup_repository(self.env, repos_path, 'merges.git',
                                     sync=False)
            reclimit = sys.getrecursionlimit()
            try:
                sys.setrecursionlimit(80)
                repos.sync()
            finally:
                sys.setrecursionlimit(reclimit)

            if self.cached_repository:
                db = self.env.get_read_db()
                cursor = db.cursor()
                cursor.execute("SELECT COUNT(repos) FROM revision "
                               "WHERE repos=%s", (repos.id,))
                rows = cursor.fetchall()
                self.assertEqual(202, rows[0][0])
                cursor.execute("SELECT time, COUNT(time) FROM revision "
                               "WHERE repos=%s GROUP BY time ORDER BY time",
                               (repos.id,))
                rows = cursor.fetchall()
                self.assertEqual((1400000000 * 1000000, 2), rows[0])
                self.assertEqual((1400000100 * 1000000, 2), rows[-1])
                self.assertEqual(
                        [(1400000000 + idx) * 1000000 for idx in xrange(101)],
                        [row[0] for row in rows])
                self.assertEqual(set([2]), set(row[1] for row in rows))
                self.assertEqual(101, len(rows))
        finally:
            rmtree(repos_path)

    def _generate_data_many_merges(self, n, timestamp=1400000000):
        init = """\
blob
mark :1
data 0

reset refs/heads/dev
commit refs/heads/dev
mark :2
author Joe <joe@example.com> %(timestamp)d +0000
committer Joe <joe@example.com> %(timestamp)d +0000
data 5
root
M 100644 :1 .gitignore

commit refs/heads/master
mark :3
author Joe <joe@example.com> %(timestamp)d +0000
committer Joe <joe@example.com> %(timestamp)d +0000
data 7
master
from :2
M 100644 :1 master.txt

"""
        merge = """\
commit refs/heads/dev
mark :%(dev)d
author Joe <joe@example.com> %(timestamp)d +0000
committer Joe <joe@example.com> %(timestamp)d +0000
data 4
dev
from :2
M 100644 :1 dev%(dev)08d.txt

commit refs/heads/master
mark :%(merge)d
author Joe <joe@example.com> %(timestamp)d +0000
committer Joe <joe@example.com> %(timestamp)d +0000
data 19
Merge branch 'dev'
from :%(from)d
merge :%(dev)d
M 100644 :1 dev%(dev)08d.txt

"""
        data = StringIO()
        data.write(init % {'timestamp': timestamp})
        for idx in xrange(n):
            data.write(merge % {'timestamp': timestamp + idx + 1,
                                'dev': 4 + idx * 2,
                                'merge': 5 + idx * 2,
                                'from': 3 + idx * 2})
        return data.getvalue()


def suite():
    global repos_path, git_bin
    git_bin = locate('git')
    suite = unittest.TestSuite()
    if pygit2 and git_bin:
        repos_path = tempfile.mkdtemp(prefix='trac-gitrepos-')
        os.rmdir(repos_path)
        for case_class, suite_class in (
                (EmptyTestCase, EmptyGitRepositoryTestSuite),
                (NormalTestCase, GitRepositoryTestSuite)):
            for cached_repository in (False, True):
                prefix = ('NonCached', 'Cached')[cached_repository]
                tc = type(prefix + case_class.__name__,
                          (case_class, GitTestCaseSetup, unittest.TestCase),
                          {'cached_repository': cached_repository})
                suite.addTest(unittest.makeSuite(tc, 'test',
                                                 suiteClass=suite_class))
    else:
        print('SKIP: %s (no git binary installed)' % __name__)
    return suite


if __name__ == '__main__':
    unittest.main(defaultTest='suite')
