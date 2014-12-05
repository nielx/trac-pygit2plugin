# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2014 Jun Omae <jun66j5@gmail.com>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

import os.path
import shutil
import tempfile
import unittest
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
from tracext.pygit2 import git_fs


REPOS_NAME = 'test.git'
REPOS_URL = 'http://example.org/git/test.git'
HEAD_REV = u'97553583461dd682de4db752f1e102fb19b019d8'
HEAD_ABBREV = u'9755358'

dumpfile_path = os.path.join(os.path.dirname(__file__), 'gitrepos.dump')
repos_path = None
git_bin = None


def spawn(*args, **kwargs):
    kw = {'stdout': PIPE, 'stderr': PIPE, 'close_fds': close_fds}
    kw.update(kwargs)
    proc = Popen(args, **kw)
    stdout, stderr = proc.communicate()
    assert proc.returncode == 0, stderr


def create_repository(path, use_dump=True):
    pygit2.init_repository(path, True)
    if use_dump:
        f = open(dumpfile_path, 'rb')
        try:
            spawn(git_bin, '--git-dir=' + path, 'fast-import', stdin=f)
        finally:
            f.close()


def setup_repository(env, path):
    provider = DbRepositoryProvider(env)
    provider.add_repository(REPOS_NAME, path, 'pygit2')
    provider.modify_repository(REPOS_NAME, {'url': REPOS_URL})
    repos = env.get_repository(REPOS_NAME)
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
        self.repos.close()
        self.repos = None
        RepositoryManager(self.env).reload_repositories()
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
                          u'fc398de')
        self.assertRaises(NoSuchChangeset, self.repos.get_changeset,
                          u'fc398de9939a675d6001f204c099215337d4eb24')

    def test_get_changesets(self):
        start = datetime(2001, 1, 1, tzinfo=utc)
        stop = datetime(2014, 1, 1, tzinfo=utc)
        changesets = self.repos.get_changesets(start, stop)
        self.assertRaises(StopIteration, changesets.next)

    def test_has_node(self):
        self.assertEquals(False, self.repos.has_node('/', '1' * 40))
        self.assertEquals(True, self.repos.has_node('/'))
        self.assertEquals(False, self.repos.has_node('/', 'fc398de'))

    def test_get_node(self):
        node = self.repos.get_node('/')
        self.assertEquals('', node.path)
        self.assertEquals('', node.created_path)
        self.assertEquals(None, node.rev)
        self.assertEquals(None, node.created_rev)
        self.assertEquals([], list(node.get_entries()))
        self.assertEquals([], list(node.get_history()))
        self.assertRaises(NoSuchChangeset, self.repos.get_node, '/', 'fc398de')
        self.assertRaises(NoSuchNode, self.repos.get_node, '/path')
        self.assertRaises(NoSuchChangeset, self.repos.get_node, '/path',
                          'fc398de')

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
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev,
                          'fc398de9939a675d6001f204c099215337d4eb24')
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, '1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.normalize_rev, '0' * 40)

    def test_get_changes(self):
        self.assertRaises(NoSuchChangeset, self.repos.get_changes,
                          '/', 'fc398de', '/', '0ee9cfd')


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
        self.assertEquals(('tags', u'ver0.1', '/',
                           'fc398de9939a675d6001f204c099215337d4eb24'),
                          entries.next())
        self.assertEquals(('tags', u'vér0.1', '/',
                           'fc398de9939a675d6001f204c099215337d4eb24'),
                          entries.next())
        self.assertRaises(StopIteration, entries.next)

    def test_get_path_url(self):
        self.assertEquals(REPOS_URL, self.repos.get_path_url('/', None))
        self.assertEquals(REPOS_URL, self.repos.get_path_url('/', 'fc398de'))
        self.assertEquals(REPOS_URL, self.repos.get_path_url('/.gitignore',
                                                             None))
        self.assertEquals(REPOS_URL, self.repos.get_path_url('/.gitignore',
                                                             'fc398de'))

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
        cset = self.repos.get_changeset('fc398de')
        self.assert_(isinstance(cset, Changeset), repr(cset))
        self.assertEquals(u'fc398de9939a675d6001f204c099215337d4eb24',
                          cset.rev)
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
                           u'dir/tété.txt',
                           u'fc398de9939a675d6001f204c099215337d4eb24'),
                          changes.next())
        self.assertEquals((u'dir2/simple-another.txt', Node.FILE,
                           Changeset.ADD, None, None),
                          changes.next())
        self.assertEquals((u'dir2/simple.txt', Node.FILE, Changeset.ADD,
                           None, None),
                          changes.next())
        # Copy root-sample.txt <- dir/sample.txt
        self.assertEquals((u'root-sample.txt', Node.FILE, Changeset.COPY,
                           u'dir/sample.txt',
                           u'fc398de9939a675d6001f204c099215337d4eb24'),
                          changes.next())
        self.assertEquals((u'root-tété.txt', Node.FILE, Changeset.EDIT,
                           u'root-tété.txt',
                           u'fc398de9939a675d6001f204c099215337d4eb24'),
                          changes.next())
        # Rename āāā-file.txt <- āāā/file.txt
        self.assertEquals((u'āāā-file.txt', Node.FILE, Changeset.MOVE,
                           u'āāā/file.txt',
                           u'fc398de9939a675d6001f204c099215337d4eb24'),
                          changes.next())
        self.assertRaises(StopIteration, changes.next)

    def test_changeset_get_branches(self):
        self.assertEquals(
            [(u'develöp', False), ('master', False), (u'stâble', False)],
            self.repos.get_changeset('fc398de').get_branches())
        self.assertEquals(
            [(u'develöp', True), ('master', True), (u'stâble', True)],
            self.repos.get_changeset(HEAD_REV[:7]).get_branches())

    def test_changeset_get_tags(self):
        self.assertEquals([u'ver0.1', u'vér0.1'],
                          self.repos.get_changeset('fc398de').get_tags())
        self.assertEquals([], self.repos.get_changeset('0ee9cfd').get_tags())

    def test_get_changeset_uid(self):
        rev = u'fc398de9939a675d6001f204c099215337d4eb24'
        self.assertEquals(rev, self.repos.get_changeset_uid(rev))

    def test_get_changesets(self):
        changesets = self.repos.get_changesets(
            datetime(2013, 2, 13, 15, 0, 0, tzinfo=utc),
            datetime(2013, 2, 14, 15, 0, 0, tzinfo=utc))
        self.assertEquals('fc398de9939a675d6001f204c099215337d4eb24',
                          changesets.next().rev)
        self.assertRaises(StopIteration, changesets.next)

        changesets = self.repos.get_changesets(
            datetime(2013, 2, 14, 14, 0, 0, tzinfo=utc),
            datetime(2013, 2, 14, 17, 0, 0, tzinfo=utc))
        self.assertEquals('0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                          changesets.next().rev)
        self.assertEquals('fc398de9939a675d6001f204c099215337d4eb24',
                          changesets.next().rev)
        self.assertRaises(StopIteration, changesets.next)

    def test_has_node(self):
        self.assertEquals(False, self.repos.has_node('/', '1' * 40))
        self.assertEquals(True, self.repos.has_node('/'))
        self.assertEquals(True, self.repos.has_node('/', '0ee9cfd'))
        self.assertEquals(True, self.repos.has_node('/'))
        self.assertEquals(True, self.repos.has_node('/.gitignore', 'fc398de'))

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
        node = self.repos.get_node(u'/dir', 'fc398de')
        self.assertEquals(u'dir', node.name)
        self.assertEquals(u'dir', node.path)
        self.assertEquals(Node.DIRECTORY, node.kind)
        self.assertEquals(u'fc398de9939a675d6001f204c099215337d4eb24',
                          node.rev)
        self.assertEquals(u'fc398de9939a675d6001f204c099215337d4eb24',
                          node.created_rev)
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
        self.assertEquals(u'fc398de9939a675d6001f204c099215337d4eb24',
                          node.created_rev)
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
        self.assertEquals((u'root-tété.txt',
                           u'fc398de9939a675d6001f204c099215337d4eb24',
                           'add'),
                          history.next())
        self.assertRaises(StopIteration, history.next)

        node = self.repos.get_node(u'/root-tété.txt')
        history = node.get_history(1)
        self.assertEquals((u'root-tété.txt',
                           u'0ee9cfd6538b7b994b94a45ed173d9d45272b0c5',
                           'edit'),
                          history.next())
        self.assertRaises(StopIteration, history.next)

        node = self.repos.get_node(u'/root-tété.txt', 'fc398de')
        history = node.get_history()
        self.assertEquals((u'root-tété.txt',
                           u'fc398de9939a675d6001f204c099215337d4eb24',
                           'add'),
                          history.next())
        self.assertRaises(StopIteration, history.next)

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
        self.assertEquals(u'fc398de9939a675d6001f204c099215337d4eb24',
                          self.repos.oldest_rev)

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

    # TODO: GitRepository.rev_older_than(self, rev1, rev2):

    def test_rev_older_than_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.rev_older_than,
                          '1' * 40, '1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.rev_older_than,
                          'fc398de9939a675d6001f204c099215337d4eb24', '1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.rev_older_than,
                          '1' * 40, 'fc398de9939a675d6001f204c099215337d4eb24')

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
        rev = 'fc398de9939a675d6001f204c099215337d4eb24'
        urev = unicode(rev)
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
        rev = 'fc398de9939a675d6001f204c099215337d4eb24'
        urev = unicode(rev)

        self.assertEquals(unicode, type(self.repos.short_rev(rev)))
        self.assertEquals(u'fc398de', self.repos.short_rev(rev))
        self.assertEquals(u'fc398de', self.repos.short_rev(rev[:7]))
        self.assertEquals(unicode, type(self.repos.short_rev(urev)))
        self.assertEquals(u'fc398de', self.repos.short_rev(urev))
        self.assertEquals(u'fc398de', self.repos.short_rev(urev[:7]))

    def test_short_rev_nonexistent(self):
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, u'1' * 40)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, True)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, False)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, 42)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, 42L)
        self.assertRaises(NoSuchChangeset, self.repos.short_rev, 4.2)

    def test_display_rev(self):
        rev = 'fc398de9939a675d6001f204c099215337d4eb24'
        urev = unicode(rev)

        self.assertEquals(unicode, type(self.repos.display_rev(rev)))
        self.assertEquals(u'fc398de', self.repos.display_rev(rev))
        self.assertEquals(u'fc398de', self.repos.display_rev(rev[:7]))
        self.assertEquals(unicode, type(self.repos.display_rev(urev)))
        self.assertEquals(u'fc398de', self.repos.display_rev(urev))
        self.assertEquals(u'fc398de', self.repos.display_rev(urev[:7]))

    def _cmp_change(self, expected, change):
        self.assertEquals(expected, (change[0] and change[0].path,
                                     change[1] and change[1].path,
                                     change[2], change[3]))

    def test_get_changes_different_revs(self):
        changes = self.repos.get_changes('/', 'fc398de', '/', '0ee9cfd')
        self._cmp_change((u'dir/tété.txt', None, Node.FILE, Changeset.DELETE),
                         changes.next())
        self._cmp_change((None, u'dir2/simple-another.txt', Node.FILE,
                          Changeset.ADD),
                         changes.next())
        self._cmp_change((None, u'dir2/simple.txt', Node.FILE, Changeset.ADD),
                         changes.next())
        # Copy root-sample.txt <- dir/sample.txt
        self._cmp_change((u'dir/sample.txt', u'root-sample.txt', Node.FILE,
                          Changeset.COPY),
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
        node = self.repos.get_node(u'/root-tété.txt', 'fc398de')
        self.assertEquals(['fc398de9939a675d6001f204c099215337d4eb24'],
                          node.get_annotations())
        node = self.repos.get_node(u'/root-tété.txt',
                                   'fc398de9939a675d6001f204c099215337d4eb24')
        self.assertEquals(['fc398de9939a675d6001f204c099215337d4eb24'],
                          node.get_annotations())


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
