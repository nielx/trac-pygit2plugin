# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2014 Jun Omae <jun66j5@gmail.com>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

import os
import posixpath
from cStringIO import StringIO
from datetime import datetime
from threading import RLock

try:
    import pygit2
except ImportError:
    pygit2 = None
    pygit2_version = None
else:
    from pygit2 import (
        GIT_OBJ_BLOB, GIT_OBJ_COMMIT, GIT_OBJ_TAG, GIT_OBJ_TREE,
        GIT_SORT_REVERSE, GIT_SORT_TIME, GIT_SORT_TOPOLOGICAL,
    )
    pygit2_version = pygit2.__version__
    if hasattr(pygit2, 'LIBGIT2_VERSION'):
        pygit2_version = '%s (compiled with libgit2 %s)' % \
                         (pygit2_version, pygit2.LIBGIT2_VERSION)

from genshi.builder import tag

from trac.core import Component, implements, TracError
from trac.env import Environment, ISystemInfoProvider
from trac.util import shorten_line
from trac.util.compat import any
from trac.util.datefmt import (
    FixedOffset, format_datetime, to_timestamp, to_utimestamp, utc,
)
from trac.util.text import exception_to_unicode, to_unicode
from trac.versioncontrol.api import (
    Changeset, Node, Repository, IRepositoryConnector, NoSuchChangeset,
    NoSuchNode,
)
from trac.versioncontrol.cache import (
    CACHE_METADATA_KEYS, CACHE_REPOSITORY_DIR, CACHE_YOUNGEST_REV,
    CachedRepository, CachedChangeset,
)
from trac.versioncontrol.web_ui import IPropertyRenderer, RenderedProperty
from trac.web.chrome import Chrome
from trac.wiki import IWikiSyntaxProvider
from trac.wiki.formatter import wiki_to_oneliner

try:
    from trac.util.datefmt import user_time
except ImportError:
    def user_time(req, func, *args, **kwargs):
        if 'tzinfo' not in kwargs:
            kwargs['tzinfo'] = getattr(req, 'tz', None)
        if 'locale' not in kwargs:
            kwargs['locale'] = getattr(req, 'locale', None)
        return func(*args, **kwargs)

try:
    from tracopt.versioncontrol.git.git_fs import GitConnector \
                                           as TracGitConnector
except ImportError:
    try:
        from tracext.git.git_fs import GitConnector as TracGitConnector
    except ImportError:
        TracGitConnector = None

from tracext.pygit2.translation import (
    BoolOption, IntOption, Option, N_, _, gettext, tag_,
)


__all__ = ['GitCachedRepository', 'GitCachedChangeset', 'GitConnector',
           'GitRepository', 'GitChangeset', 'GitNode']

_filemode_submodule = 0160000
_diff_find_rename_limit = 200

if pygit2:
    _status_map = {'A': Changeset.ADD, 'D': Changeset.DELETE,
                   'M': Changeset.EDIT, 'R': Changeset.MOVE,
                   'C': Changeset.COPY}
    if hasattr(pygit2.TreeEntry, 'filemode'):
        _get_filemode = lambda tree_entry: tree_entry.filemode
    else:
        _get_filemode = lambda tree_entry: tree_entry.attributes
    _walk_flags = GIT_SORT_TIME
    if not hasattr(pygit2.Patch, 'delta'):  # prior to v0.22.1
        def _iter_changes_from_diff(diff):
            for patch in diff:
                yield patch.old_file_path, patch.new_file_path, patch.status
    else:
        def _iter_changes_from_diff(diff):
            for patch in diff:
                delta = patch.delta
                yield delta.old_file.path, delta.new_file.path, delta.status
else:
    _status_map = {}
    _get_filemode = None
    _walk_flags = 0
    _iter_changes_from_diff = None


_inverted_kindmap = {Node.DIRECTORY: 'D', Node.FILE: 'F'}
_inverted_actionmap = {Changeset.ADD: 'A', Changeset.COPY: 'C',
                       Changeset.DELETE: 'D', Changeset.EDIT: 'E',
                       Changeset.MOVE: 'M'}


if hasattr(Environment, 'db_exc'):
    def _db_exc(env):
        return env.db_exc
else:
    def _db_exc(env):
        uri = env.config.get('trac', 'database', '')
        if uri.startswith('sqlite:'):
            from trac.db.sqlite_backend import sqlite
            return sqlite
        if uri.startswith('postgres:'):
            from trac.db.postgres_backend import psycopg
            return psycopg
        if uri.startswith('mysql:'):
            from trac.db.mysql_backend import MySQLdb
            return MySQLdb
        raise ValueError('Unsupported database "%s"' % uri.split(':')[0])


class GitCachedRepository(CachedRepository):
    """Git-specific cached repository."""

    has_linear_changesets = False

    def short_rev(self, rev):
        return self.repos.short_rev(rev)

    def display_rev(self, rev):
        return self.short_rev(rev)

    def normalize_rev(self, rev):
        return self.repos.normalize_rev(rev)

    def get_changeset(self, rev):
        return GitCachedChangeset(self, self.normalize_rev(rev), self.env)

    def get_node(self, path, rev=None):
        return self.repos.get_node(path, rev=rev)

    def get_youngest_rev(self):
        # return None if repository is empty
        return CachedRepository.get_youngest_rev(self) or None

    def get_quickjump_entries(self, rev):
        try:
            rev = self.normalize_rev(rev)
        except NoSuchChangeset:
            return ()
        else:
            return self.repos.get_quickjump_entries(rev)

    def has_node(self, path, rev=None):
        try:
            self.get_node(path, rev=rev)
        except (NoSuchChangeset, NoSuchNode):
            return False
        else:
            return True

    def parent_revs(self, rev):
        return self.repos.parent_revs(rev)

    def child_revs(self, rev):
        return self.repos.child_revs(rev)

    def sync(self, feedback=None, clean=False):
        if clean:
            self.remove_cache()

        metadata = self.metadata
        self.save_metadata(metadata)
        meta_youngest = metadata.get(CACHE_YOUNGEST_REV, '')
        repos = self.repos
        git_repos = repos.git_repos
        db = self.env.get_read_db()

        IntegrityError = _db_exc(self.env).IntegrityError
        cursor = db.cursor()

        def is_synced(rev):
            cursor.execute("SELECT COUNT(repos) FROM revision "
                           "WHERE repos=%s AND rev=%s",
                           (self.id, rev))
            row = cursor.fetchone()
            return row[0] > 0

        def traverse(commit, seen):
            commits = []
            merges = []
            while True:
                rev = commit.hex
                if rev in seen:
                    break
                seen.add(rev)
                if is_synced(rev):
                    break
                commits.append(commit)
                parents = commit.parents
                if not parents:  # root commit?
                    break
                commit = parents[0]
                if len(parents) > 1:
                    merges.append((len(commits), parents[1:]))
            for idx, parents in reversed(merges):
                for parent in parents:
                    commits[idx:idx] = traverse(parent, seen)
            return commits

        while True:
            repos_youngest = repos.youngest_rev or ''
            updated = [False]
            seen = set()

            for name in git_repos.listall_references():
                ref = git_repos.lookup_reference(name)
                git_object = ref.get_object()
                type_ = git_object.type
                if type_ == GIT_OBJ_TAG:
                    git_object = git_object.get_object()
                    type_ = git_object.type
                if type_ != GIT_OBJ_COMMIT:
                    continue

                commits = traverse(git_object, seen)  # topology ordered
                while commits:
                    # sync revision from older revision to newer revision
                    commit = commits.pop()
                    rev = commit.hex
                    self.log.info("Trying to sync revision [%s]", rev)
                    cset = GitChangeset(repos, commit)
                    @self.env.with_transaction()
                    def do_insert(db):
                        try:
                            self._insert_cset(db, rev, cset)
                            updated[0] = True
                        except IntegrityError, e:
                            self.log.info('Revision %s already cached: %r',
                                          rev, e)
                            db.rollback()
                    if feedback:
                        feedback(rev)

            if updated[0]:
                continue  # sync again

            if meta_youngest != repos_youngest:
                @self.env.with_transaction()
                def update_metadata(db):
                    cursor = db.cursor()
                    cursor.execute("""
                        UPDATE repository SET value=%s WHERE id=%s AND name=%s
                        """, (repos_youngest, self.id, CACHE_YOUNGEST_REV))
                    del self.metadata
            return

    if not hasattr(CachedRepository, 'remove_cache'):
        def remove_cache(self):
            self.log.info("Cleaning cache")
            @self.env.with_transaction()
            def fn(db):
                cursor = db.cursor()
                cursor.execute("DELETE FROM revision WHERE repos=%s",
                               (self.id,))
                cursor.execute("DELETE FROM node_change WHERE repos=%s",
                               (self.id,))
                cursor.executemany(
                    "DELETE FROM repository WHERE id=%s AND name=%s",
                    [(self.id, k) for k in CACHE_METADATA_KEYS])
                cursor.executemany("""
                    INSERT INTO repository (id, name, value)
                    VALUES (%s, %s, %s)
                    """, [(self.id, k, '') for k in CACHE_METADATA_KEYS])
                del self.metadata

    if not hasattr(CachedRepository, 'save_metadata'):
        def save_metadata(self, metadata):
            @self.env.with_transaction()
            def fn(db):
                invalidate = False
                cursor = db.cursor()

                # -- check that we're populating the cache for the correct
                #    repository
                repository_dir = metadata.get(CACHE_REPOSITORY_DIR)
                if repository_dir:
                    # directory part of the repo name can vary on case
                    # insensitive fs
                    if os.path.normcase(repository_dir) \
                            != os.path.normcase(self.name):
                        self.log.info("'repository_dir' has changed from %r "
                                      "to %r", repository_dir, self.name)
                        raise TracError(_(
                            "The repository directory has changed, you should "
                            "resynchronize the repository with: trac-admin "
                            "$ENV repository resync '%(reponame)s'",
                            reponame=self.reponame or '(default)'))
                elif repository_dir is None: #
                    self.log.info('Storing initial "repository_dir": %s',
                                  self.name)
                    cursor.execute("INSERT INTO repository (id, name, value) "
                                   "VALUES (%s, %s, %s)",
                                   (self.id, CACHE_REPOSITORY_DIR, self.name))
                    invalidate = True
                else: # 'repository_dir' cleared by a resync
                    self.log.info('Resetting "repository_dir": %s', self.name)
                    cursor.execute("UPDATE repository SET value=%s "
                                   "WHERE id=%s AND name=%s",
                                   (self.name, self.id, CACHE_REPOSITORY_DIR))
                    invalidate = True

                # -- insert a 'youngeset_rev' for the repository if necessary
                if CACHE_YOUNGEST_REV not in metadata:
                    cursor.execute("INSERT INTO repository (id, name, value) "
                                   "VALUES (%s, %s, %s)",
                                   (self.id, CACHE_YOUNGEST_REV, ''))
                    invalidate = True

                if invalidate:
                    del self.metadata

    if hasattr(CachedRepository, 'insert_changeset'):
        def _insert_cset(self, db, rev, cset):
            return self.insert_changeset(rev, cset)
    else:
        def _insert_cset(self, db, rev, cset):
            cursor = db.cursor()
            srev = self.db_rev(rev)
            cursor.execute("""
                INSERT INTO revision (repos,rev,time,author,message)
                VALUES (%s,%s,%s,%s,%s)
                """, (self.id, srev, to_utimestamp(cset.date),
                      cset.author, cset.message))
            for path, kind, action, bpath, brev in cset.get_changes():
                self.log.debug("Caching node change in [%s]: %r", rev,
                               (path, kind, action, bpath, brev))
                kind = _inverted_kindmap[kind]
                action = _inverted_actionmap[action]
                cursor.execute("""
                    INSERT INTO node_change
                        (repos,rev,path,node_type,change_type,base_path,
                         base_rev)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """, (self.id, srev, path, kind, action, bpath, brev))


class GitCachedChangeset(CachedChangeset):
    """Git-specific cached changeset."""

    def get_branches(self):
        return self.repos.repos._get_branches_cset(self.rev)

    def get_tags(self):
        return self.repos.repos._get_tags_cset(self.rev)


def intersperse(sep, iterable):
    """The 'intersperse' generator takes an element and an iterable and
    intersperses that element between the elements of the iterable.

    inspired by Haskell's ``Data.List.intersperse``
    """

    for i, item in enumerate(iterable):
        if i:
            yield sep
        yield item


def _git_timestamp(ts, offset):
    if offset == 0:
        tz = utc
    else:
        hours, rem = divmod(abs(offset), 60)
        tzname = 'UTC%+03d:%02d' % ((hours, -hours)[offset < 0], rem)
        tz = FixedOffset(offset, tzname)
    return datetime.fromtimestamp(ts, tz)


def _format_signature(signature):
    name = signature.name.strip()
    email = signature.email.strip()
    return ('%s <%s>' % (name, email)).strip()


def _walk_tree(repos, tree, path=None):
    for entry in tree:
        if _get_filemode(entry) == _filemode_submodule:
            continue
        git_object = repos.get(entry.oid)
        if git_object is None:
            continue
        if path is not None:
            name = posixpath.join(path, entry.name)
        else:
            name = entry.name
        if git_object.type == GIT_OBJ_TREE:
            for val in _walk_tree(repos, git_object, name):
                yield val
        else:
            yield git_object, name


class _CachedWalker(object):

    __slots__ = ('rev', 'walker', 'revs', 'commits', '_lock')

    def __init__(self, git_repos, rev, flags=_walk_flags):
        self.rev = rev
        self.walker = git_repos.walk(rev, flags)
        self.revs = set()
        self.commits = []
        self._lock = RLock()

    def __contains__(self, rev):
        self._lock.acquire()
        try:
            if rev in self.revs:
                return True
            add_rev = self.revs.add
            add_commit = self.commits.append
            for commit in self.walker:
                old_rev = commit.hex
                add_commit(commit)
                add_rev(old_rev)
                if old_rev == rev:
                    return True
            return False
        finally:
            self._lock.release()

    def reverse(self, start_rev):
        self._lock.acquire()
        try:
            if start_rev in self:
                commits = self.commits
                idx = len(commits) - 1
                for idx in xrange(len(commits) - 1, -1, -1):
                    commit = commits[idx]
                    if commit.hex == start_rev:
                        return reversed(commits[0:idx + 1])
            return ()
        finally:
            self._lock.release()


class GitConnector(Component):

    implements(ISystemInfoProvider, IRepositoryConnector, IWikiSyntaxProvider)

    # ISystemInfoProvider methods

    def get_system_info(self):
        if pygit2:
            yield 'pygit2', pygit2_version

    # IWikiSyntaxProvider methods

    def _format_sha_link(self, formatter, rev, label):
        # FIXME: this function needs serious rethinking...

        reponame = ''

        context = formatter.context
        while context:
            if context.resource.realm in ('source', 'changeset'):
                reponame = context.resource.parent.id
                break
            context = context.parent

        try:
            repos = self.env.get_repository(reponame)

            if not repos:
                raise Exception("Repository '%s' not found" % reponame)

            rev = repos.normalize_rev(rev)  # in case it was abbreviated
            changeset = repos.get_changeset(rev)
            return tag.a(label, class_='changeset',
                         title=shorten_line(changeset.message),
                         href=formatter.href.changeset(rev, repos.reponame))
        except Exception, e:
            return tag.a(label, class_='missing changeset',
                         title=to_unicode(e), rel='nofollow')

    def get_wiki_syntax(self):
        yield (r'(?:\b|!)r?[0-9a-fA-F]{%d,40}\b' % self.wiki_shortrev_len,
               lambda fmt, rev, match:
                    self._format_sha_link(fmt, rev.startswith('r')
                                          and rev[1:] or rev, rev))

    def get_link_resolvers(self):
        return ()

    if TracGitConnector:
        @property
        def cached_repository(self):
            return TracGitConnector(self.env).cached_repository

        @property
        def shortrev_len(self):
            return TracGitConnector(self.env).shortrev_len

        @property
        def wiki_shortrev_len(self):
            return TracGitConnector(self.env).wiki_shortrev_len

        @property
        def trac_user_rlookup(self):
            return TracGitConnector(self.env).trac_user_rlookup

        @property
        def use_committer_id(self):
            return TracGitConnector(self.env).use_committer_id

        @property
        def use_committer_time(self):
            return TracGitConnector(self.env).use_committer_time

        @property
        def git_fs_encoding(self):
            return TracGitConnector(self.env).git_fs_encoding
    else:
        cached_repository = BoolOption('git', 'cached_repository', 'false',
            N_("Wrap `GitRepository` in `CachedRepository`."))

        shortrev_len = IntOption('git', 'shortrev_len', 7,
            N_("The length at which a sha1 should be abbreviated to (must be "
               ">= 4 and <= 40)."))

        wiki_shortrev_len = IntOption('git', 'wikishortrev_len', 40,
            N_("The minimum length of an hex-string for which auto-detection "
               "as sha1 is performed (must be >= 4 and <= 40)."))

        trac_user_rlookup = BoolOption('git', 'trac_user_rlookup', 'false',
            N_("Enable reverse mapping of git email addresses to trac user "
               "ids (costly if you have many users)."))

        use_committer_id = BoolOption('git', 'use_committer_id', 'true',
            N_("Use git-committer id instead of git-author id for the "
               "changeset ''Author'' field."))

        use_committer_time = BoolOption('git', 'use_committer_time', 'true',
            N_("Use git-committer timestamp instead of git-author timestamp "
               "for the changeset ''Timestamp'' field."))

        git_fs_encoding = Option('git', 'git_fs_encoding', 'utf-8',
            N_("Define charset encoding of paths within git repositories."))

    # IRepositoryConnector methods

    def get_supported_types(self):
        if pygit2:
            yield 'git', 4  # lower priority than tracopt.v.git
            yield 'pygit2', 8
            yield 'direct-pygit2', 8
            yield 'cached-pygit2', 8

    def get_repository(self, type, dir, params):
        """GitRepository factory method"""
        assert type in ('git', 'pygit2', 'direct-pygit2', 'cached-pygit2')

        if not (4 <= self.shortrev_len <= 40):
            raise TracError(_("[git] shortrev_len setting must be within "
                              "[4..40]"))

        if not (4 <= self.wiki_shortrev_len <= 40):
            raise TracError(_("[git] wikishortrev_len must be within [4..40]"))

        if self.trac_user_rlookup:
            format_signature = self._format_signature_by_email
        else:
            format_signature = None

        repos = GitRepository(dir, params, self.log,
                              git_fs_encoding=self.git_fs_encoding,
                              shortrev_len=self.shortrev_len,
                              format_signature=format_signature,
                              use_committer_id=self.use_committer_id,
                              use_committer_time=self.use_committer_time)

        if type == 'cached-pygit2':
            use_cached = True
        elif type == 'direct-pygit2':
            use_cached = False
        else:
            use_cached = self.cached_repository
        if use_cached:
            repos = GitCachedRepository(self.env, repos, self.log)
            self.log.debug("enabled CachedRepository for '%s'", dir)
        else:
            self.log.debug("disabled CachedRepository for '%s'", dir)

        return repos

    def _format_signature_by_email(self, signature):
        """Reverse map 'real name <user@domain.tld>' addresses to trac
        user ids.
        """
        email = (signature.email or '').strip()
        if email:
            email = email.lower()
            for username, name, _email in self.env.get_known_users():
                if _email and email == _email.lower():
                    return username
        return _format_signature(signature)


class CsetPropertyRenderer(Component):

    implements(IPropertyRenderer)

    git_properties = (
        N_("Parents:"), N_("Children:"), N_("Branches:"), N_("Tags:"),
    )

    # relied upon by GitChangeset
    def match_property(self, name, mode):
        if (mode == 'revprop' and
            name.startswith('git-') and
            name[4:] in ('Parents', 'Children', 'Branches', 'Tags',
                         'committer', 'author')):
            return 4
        return 0

    def render_property(self, name, mode, context, props):
        if name.startswith('git-'):
            label = name[4:] + ':'
            if label in self.git_properties:
                label = gettext(label)
        else:
            label = name
        return RenderedProperty(
                name=label, name_attributes=[('class', 'property')],
                content=self._render_property(name, props[name], context))

    def _render_property(self, name, value, context):
        if name == 'git-Branches':
            return self._render_branches(context, value)

        if name == 'git-Tags':
            return self._render_tags(context, value)

        if name == 'git-Parents' and len(value) > 1:
            return self._render_merge_commit(context, value)

        if name in ('git-Parents', 'git-Children'):
            return self._render_revs(context, value)

        if name in ('git-committer', 'git-author'):
            return self._render_signature(context, value)

        raise TracError("Internal error")

    def _render_branches(self, context, branches):
        links = [self._changeset_link(context, rev, name)
                 for name, rev in branches]
        return tag(*intersperse(', ', links))

    def _render_tags(self, context, names):
        rev = context.resource.id
        links = [self._changeset_link(context, rev, name) for name in names]
        return tag(*intersperse(', ', links))

    def _render_revs(self, context, revs):
        links = [self._changeset_link(context, rev) for rev in revs]
        return tag(*intersperse(', ', links))

    def _render_merge_commit(self, context, revs):
        # we got a merge...
        curr_rev = context.resource.id
        reponame = context.resource.parent.id
        href = context.href

        def parent_diff(rev):
            link = self._changeset_link(context, rev)
            diff = tag.a(_("diff"),
                         href=href.changeset(curr_rev, reponame, old=rev),
                         title=_("Diff against this parent (show the changes "
                                 "merged from the other parents)"))
            return tag_("%(rev)s (%(diff)s)", rev=link, diff=diff)

        links = intersperse(', ', map(parent_diff, revs))
        hint = wiki_to_oneliner(
            _("'''Note''': this is a '''merge''' changeset, the changes "
              "displayed below correspond to the merge itself. Use the "
              "`(diff)` links above to see all the changes relative to each "
              "parent."),
            self.env)
        return tag(list(links), tag.br(), tag.span(hint, class_='hint'))

    def _render_signature(self, context, signature):
        req = context.req
        dt = _git_timestamp(signature.time, signature.offset)
        chrome = Chrome(self.env)
        return u'%s (%s)' % (chrome.format_author(req, signature.name),
                             user_time(req, format_datetime, dt))

    def _changeset_link(self, context, rev, label=None):
        # `rev` is assumed to be a non-abbreviated 40-chars sha id
        reponame = context.resource.parent.id
        repos = self.env.get_repository(reponame)
        try:
            cset = repos.get_changeset(rev)
        except (NoSuchChangeset, NoSuchNode), e:
            return tag.a(rev, class_='missing changeset', title=to_unicode(e),
                         rel='nofollow')
        if label is None:
            label = repos.display_rev(rev)
        return tag.a(label, class_='changeset',
                     title=shorten_line(cset.message),
                     href=context.href.changeset(rev, repos.reponame))


class GitRepository(Repository):
    """Git repository"""

    has_linear_changesets = False

    def __init__(self, path, params, log, git_fs_encoding='utf-8',
                 shortrev_len=7, format_signature=None, use_committer_id=False,
                 use_committer_time=False):

        try:
            self.git_repos = pygit2.Repository(path)
        except Exception, e:
            log.warn('Not git repository: %r (%s)', path,
                     exception_to_unicode(e))
            raise TracError(_("%(path)s does not appear to be a Git "
                              "repository.", path=path))

        self.path = path
        self.params = params
        self.git_fs_encoding = git_fs_encoding
        self.shortrev_len = max(4, min(shortrev_len, 40))
        self.format_signature = format_signature or _format_signature
        self.use_committer_id = use_committer_id
        self.use_committer_time = use_committer_time
        self._ref_walkers = {}
        Repository.__init__(self, 'git:' + path, self.params, log)

    def _from_fspath(self, name):
        return name.decode(self.git_fs_encoding)

    def _to_fspath(self, name):
        return name.encode(self.git_fs_encoding)

    def _stringify_rev(self, rev):
        if rev is not None and not isinstance(rev, unicode):
            rev = to_unicode(rev)
        return rev

    def _get_commit_username(self, commit):
        if self.use_committer_id:
            signature = commit.committer or commit.author
        else:
            signature = commit.author or commit.committer
        return self.format_signature(signature)

    def _get_commit_time(self, commit):
        if self.use_committer_time:
            signature = commit.committer or commit.author
        else:
            signature = commit.author or commit.committer
        return _git_timestamp(signature.time, signature.offset)

    def _get_tree_entry(self, tree, path):
        if not path:
            return None
        if isinstance(path, unicode):
            path = self._to_fspath(path)
        entry = tree
        for name in path.split('/'):
            if tree is None or tree.type != GIT_OBJ_TREE or name not in tree:
                return None
            entry = tree[name]
            tree = self.git_repos.get(entry.oid)
        return entry

    def _get_tree(self, tree, path):
        if not path:
            return tree
        entry = self._get_tree_entry(tree, path)
        if entry:
            return self.git_repos.get(entry.oid)
        return None

    def _get_commit(self, oid):
        git_repos = self.git_repos
        try:
            git_object = git_repos[oid]
        except (KeyError, ValueError):
            return None
        if git_object.type == GIT_OBJ_TAG:
            git_object = git_object.get_object()
        if git_object.type == GIT_OBJ_COMMIT:
            return git_object
        return None

    def _iter_ref_walkers(self, rev):
        git_repos = self.git_repos
        target = git_repos[rev]

        walkers = self._ref_walkers
        for name in git_repos.listall_references():
            if not name.startswith('refs/heads/'):
                continue
            ref = git_repos.lookup_reference(name)
            commit = self._get_commit(ref.target)
            if not commit:
                continue
            if commit.commit_time < target.commit_time:
                continue
            walker = walkers.get(name)
            if walker and walker.rev != commit.hex:
                walker = None
            if not walker:
                walkers[name] = walker = _CachedWalker(git_repos, commit.hex)
            yield self._from_fspath(name), ref, walker

    def _get_changes(self, parent_tree, commit_tree):
        diff = parent_tree.diff_to_tree(commit_tree)
        # don't detect rename if the diff has too many files
        if len(diff) <= _diff_find_rename_limit or \
                sum(patch.status == 'A'
                    for patch in diff) <= _diff_find_rename_limit:
            diff.find_similar()
        _from_fspath = self._from_fspath
        generator = ((_from_fspath(old_path), _from_fspath(new_path), status)
                     for old_path, new_path, status
                     in _iter_changes_from_diff(diff) if status in _status_map)
        return sorted(generator, key=lambda item: item[1])

    def _get_branches(self, rev):
        return sorted((name[11:], ref.target.hex)
                      for name, ref, walker in self._iter_ref_walkers(rev)
                      if rev in walker)

    def _get_branches_cset(self, rev):
        return [(name, r == rev) for name, r in self._get_branches(rev)]

    def _get_tags_cset(self, rev):
        git_repos = self.git_repos
        _from_fspath = self._from_fspath

        def iter_tags():
            for name in git_repos.listall_references():
                if not name.startswith('refs/tags/'):
                    continue
                ref = git_repos.lookup_reference(name)
                git_object = ref.get_object()
                if git_object.type == GIT_OBJ_TAG:
                    git_object = git_object.get_object()
                if rev == git_object.hex:
                    yield _from_fspath(name[10:])

        return sorted(iter_tags())

    def _resolve_rev(self, rev, raises=True):
        git_repos = self.git_repos
        rev = self._stringify_rev(rev)
        if not rev:
            try:
                return git_repos.get(git_repos.head.target)
            except pygit2.GitError:
                if raises:
                    raise NoSuchChangeset(rev)
                return None

        commit = self._get_commit(rev)
        if commit:
            return commit

        for name in git_repos.listall_references():
            ref = git_repos.lookup_reference(name)
            name = self._from_fspath(name)
            if name.startswith('refs/heads/'):
                match = name[11:] == rev
            elif name.startswith('refs/tags/'):
                match = name[10:] == rev
            else:
                continue
            if not match:
                continue
            commit = self._get_commit(ref.target)
            if commit:
                return commit

        if raises:
            raise NoSuchChangeset(rev)

    def close(self):
        self._ref_walkers.clear()
        self.git_repos = None

    def get_youngest_rev(self):
        try:
            return self.git_repos.head.target.hex
        except pygit2.GitError:
            return None

    def get_oldest_rev(self):
        try:
            self.git_repos.head
        except pygit2.GitError:
            return None
        for commit in self.git_repos.walk(self.git_repos.head.target,
                                          _walk_flags | GIT_SORT_REVERSE):
            return commit.hex

    def normalize_path(self, path):
        if isinstance(path, str):
            path = self._from_fspath(path)
        if path:
            return path.strip('/')
        else:
            return ''

    def normalize_rev(self, rev):
        return to_unicode(self._resolve_rev(rev).oid.hex)

    def short_rev(self, rev):
        rev = self.normalize_rev(rev)
        git_repos = self.git_repos
        for size in xrange(self.shortrev_len, 40):
            short_rev = rev[:size]
            try:
                git_object = git_repos[short_rev]
                if git_object.type == GIT_OBJ_COMMIT:
                    return short_rev
            except (KeyError, ValueError):
                pass
        return rev

    def display_rev(self, rev):
        return self.short_rev(rev)

    def get_node(self, path, rev=None):
        rev = self._stringify_rev(rev)
        commit = self._resolve_rev(rev, raises=False)
        if commit is None and rev:
            raise NoSuchChangeset(rev)
        return GitNode(self, self.normalize_path(path), commit)

    def get_quickjump_entries(self, rev):
        git_repos = self.git_repos
        refs = sorted(
            (self._from_fspath(name), git_repos.lookup_reference(name))
            for name in git_repos.listall_references()
            if name.startswith('refs/heads/') or name.startswith('refs/tags/'))

        for name, ref in refs:
            if name.startswith('refs/heads/'):
                commit = self._get_commit(ref.target)
                if commit:
                    yield 'branches', name[11:], '/', commit.hex

        for name, ref in refs:
            if name.startswith('refs/tags/'):
                commit = self._get_commit(ref.target)
                yield 'tags', name[10:], '/', commit.hex

    def get_path_url(self, path, rev):
        return self.params.get('url')

    def get_changesets(self, start, stop):
        seen_oids = set()

        def iter_commits():
            ts_start = to_timestamp(start)
            ts_stop = to_timestamp(stop)
            git_repos = self.git_repos
            for name in git_repos.listall_references():
                if not name.startswith('refs/heads/'):
                    continue
                ref = git_repos.lookup_reference(name)
                for commit in git_repos.walk(ref.target, _walk_flags):
                    ts = commit.commit_time
                    if ts < ts_start:
                        break
                    if ts_start <= ts <= ts_stop:
                        oid = commit.oid
                        if oid not in seen_oids:
                            seen_oids.add(oid)
                            yield ts, commit

        for ts, commit in sorted(iter_commits(), key=lambda v: v[0],
                                 reverse=True):
            yield GitChangeset(self, commit)

    def get_changeset(self, rev):
        return GitChangeset(self, self._resolve_rev(rev))

    def get_changeset_uid(self, rev):
        return rev

    def get_changes(self, old_path, old_rev, new_path, new_rev,
                    ignore_ancestry=0):
        # TODO: handle ignore_ancestry

        def iter_changes(old_commit, old_path, new_commit, new_path):
            old_tree = self._get_tree(old_commit.tree, old_path)
            old_rev = old_commit.hex
            new_tree = self._get_tree(new_commit.tree, new_path)
            new_rev = new_commit.hex

            for old_file, new_file, status in \
                    self._get_changes(old_tree, new_tree):
                action = _status_map.get(status)
                if not action:
                    continue
                old_node = new_node = None
                if status != 'A':
                    old_node = self.get_node(
                                posixpath.join(old_path, old_file), old_rev)
                if status != 'D':
                    new_node = self.get_node(
                                posixpath.join(new_path, new_file), new_rev)
                yield old_node, new_node, Node.FILE, action

        old_commit = self._resolve_rev(old_rev)
        new_commit = self._resolve_rev(new_rev)
        return iter_changes(old_commit, self.normalize_path(old_path),
                            new_commit, self.normalize_path(new_path))

    def previous_rev(self, rev, path=''):
        commit = self._resolve_rev(rev)
        if not path or path == '/':
            for parent in commit.parents:
                return parent.hex
        else:
            node = GitNode(self, self.normalize_path(path), commit)
            for commit, action in node._walk_commits():
                for parent in commit.parents:
                    return parent.hex

    def next_rev(self, rev, path=''):
        rev = self.normalize_rev(rev)
        path = self._to_fspath(self.normalize_path(path))

        for name, ref, walker in self._iter_ref_walkers(rev):
            if rev not in walker:
                continue
            for commit in walker.reverse(rev):
                if not any(p.hex == rev for p in commit.parents):
                    continue
                tree = commit.tree
                entry = self._get_tree(tree, path)
                if entry is None:
                    return None
                for parent in commit.parents:
                    parent_tree = parent.tree
                    if tree.oid == parent_tree.oid:
                        continue
                    parent_entry = self._get_tree(parent_tree, path)
                    if entry is None or parent_entry is None or \
                            entry.oid != parent_entry.oid:
                        return commit.hex
                rev = commit.hex

    def parent_revs(self, rev):
        commit = self._resolve_rev(rev)
        return [c.hex for c in commit.parents]

    def child_revs(self, rev):
        def iter_children(rev):
            seen = set()
            for name, ref, walker in self._iter_ref_walkers(rev):
                if rev not in walker:
                    continue
                for commit in walker.reverse(rev):
                    if commit.oid in seen:
                        break
                    seen.add(commit.oid)
                    if any(p.hex == rev for p in commit.parents):
                        yield commit
        return [c.hex for c in iter_children(self.normalize_rev(rev))]

    def rev_older_than(self, rev1, rev2):
        oid1 = self._resolve_rev(rev1).oid
        oid2 = self._resolve_rev(rev2).oid
        if oid1 == oid2:
            return False
        return any(oid1 == commit.oid
                   for commit in self.git_repos.walk(oid2, _walk_flags))

    def get_path_history(self, path, rev=None, limit=None):
        raise TracError(_("GitRepository does not support path_history"))


class GitNode(Node):

    def __init__(self, repos, path, rev, created_commit=None):
        self.log = repos.log

        if type(rev) is pygit2.Commit:
            commit = rev
            rev = commit.hex
        else:
            if rev is not None and not isinstance(rev, unicode):
                rev = to_unicode(rev)
            commit = repos._resolve_rev(rev, raises=False)
            if commit is None and rev:
                raise NoSuchChangeset(rev)

        tree_entry = None
        filemode = None
        tree = None
        blob = None
        if commit:
            normrev = commit.hex
            git_object = commit.tree
            if path:
                tree_entry = repos._get_tree_entry(git_object, path)
                if tree_entry is None:
                    raise NoSuchNode(path, rev)
                filemode = _get_filemode(tree_entry)
                if filemode == _filemode_submodule:
                    git_object = None
                else:
                    git_object = repos.git_repos.get(tree_entry.oid)
            if git_object is None:
                if filemode == _filemode_submodule:
                    kind = Node.DIRECTORY
                else:
                    kind = None
            elif git_object.type == GIT_OBJ_TREE:
                kind = Node.DIRECTORY
                tree = git_object
            elif git_object.type == GIT_OBJ_BLOB:
                kind = Node.FILE
                blob = git_object
            if kind is None:
                raise NoSuchNode(path, rev)
        else:
            if path:
                raise NoSuchNode(path, rev)
            normrev = None
            kind = Node.DIRECTORY

        self.commit = commit
        self.tree_entry = tree_entry
        self.tree = tree
        self.blob = blob
        self.filemode = filemode
        self.created_path = path  # XXX how to use?
        self._created_commit = created_commit
        Node.__init__(self, repos, path, normrev, kind)

    def _get_created_commit(self):
        commit = self._created_commit
        if commit is None and self.commit and self.rev:
            _get_tree_entry = self.repos._get_tree_entry
            path = self.repos._to_fspath(self.path)
            commit = self.commit
            entry = _get_tree_entry(commit.tree, path)
            parents = commit.parents
            if parents:
                parent_entry = _get_tree_entry(parents[0].tree, path)
                if parent_entry is not None and parent_entry.oid == entry.oid:
                    commit = None
                    for commit, action in self._walk_commits():
                        break
            self._created_commit = commit or self.commit
        return commit

    @property
    def created_rev(self):
        commit = self._get_created_commit()
        if commit is None:
            return None
        return commit.hex

    def _walk_commits(self):
        skip_merges = self.isfile
        _get_tree = self.repos._get_tree
        path = self.repos._to_fspath(self.path)
        parent = parent_tree = None
        for commit in self.repos.git_repos.walk(self.rev, _walk_flags):
            if parent is not None and parent.oid == commit.oid:
                tree = parent_tree
            else:
                tree = _get_tree(commit.tree, path)
            parents = commit.parents
            n_parents = len(parents)
            if skip_merges and n_parents > 1:
                continue
            if n_parents == 0:
                if tree is not None:
                    yield commit, Changeset.ADD
                return
            parent = parents[0]
            parent_tree = _get_tree(parent.tree, path)
            if tree is None:
                if parent_tree is None:
                    continue
                action = Changeset.DELETE
            elif parent_tree is None:
                action = Changeset.ADD
            elif parent_tree.oid != tree.oid:
                action = Changeset.EDIT
            else:
                continue
            yield commit, action

    def get_content(self):
        if not self.isfile:
            return None
        return StringIO(self.blob.data)

    def get_properties(self):
        props = {}
        if self.filemode is not None:
            props['mode'] = '%06o' % self.filemode
        return props

    def get_annotations(self):
        if not self.isfile:
            return
        annotations = []
        for hunk in self.repos.git_repos.blame(
                self.repos._to_fspath(self.path),
                newest_commit=self.commit.oid):
            commit_id = str(hunk.final_commit_id)
            annotations.extend([commit_id] * hunk.lines_in_hunk)
        return annotations

    def get_entries(self):
        if self.commit is None or self.tree is None or not self.isdir:
            return

        repos = self.repos
        git_repos = repos.git_repos
        _get_tree = repos._get_tree
        _from_fspath = repos._from_fspath
        path = repos._to_fspath(self.path)
        names = sorted(entry.name for entry in self.tree)

        def get_entries(commit):
            tree = _get_tree(commit.tree, path)
            if tree is None:
                tree = ()
            return dict((entry.name, entry) for entry in tree)

        def is_blob(entry):
            if entry:
                return git_repos[entry.oid].type == GIT_OBJ_BLOB
            else:
                return True

        def get_commits():
            commits = {}
            parent = parent_entries = None
            for commit in git_repos.walk(self.rev, _walk_flags):
                parents = commit.parents
                n_parents = len(parents)
                if n_parents == 0:
                    break
                parent = parents[0]
                if not parent and parent.oid == commit.oid:
                    curr_entries = parent_entries
                else:
                    curr_entries = get_entries(commit)
                parent_entries = get_entries(parent)
                for name in names:
                    if name in commits:
                        continue
                    curr_entry = curr_entries.get(name)
                    parent_entry = parent_entries.get(name)
                    if not curr_entry and not parent_entry:
                        continue
                    object_changed = not curr_entry or not parent_entry or \
                                     curr_entry.oid != parent_entry.oid
                    if n_parents > 1 and object_changed and \
                            is_blob(curr_entry) and is_blob(parent_entry):
                        continue  # skip merge-commit if blob
                    if object_changed:
                        commits[name] = commit
                if len(commits) == len(names):
                    break
            return commits

        commits = get_commits()
        for name in names:
            yield GitNode(repos, posixpath.join(self.path, _from_fspath(name)),
                          self.commit, created_commit=commits.get(name))

    def get_content_type(self):
        if self.isdir:
            return None
        return ''

    def get_content_length(self):
        if not self.isfile:
            return None
        return self.blob.size

    def get_history(self, limit=None):
        path = self.path
        count = 0
        for commit, action in self._walk_commits():
            yield path, commit.hex, action
            count += 1
            if limit == count:
                return

    def get_last_modified(self):
        if not self.isfile:
            return None
        commit = self._get_created_commit()
        if commit is None:
            return None
        return self.repos._get_commit_time(commit)


class GitChangeset(Changeset):
    """A Git changeset in the Git repository.

    Corresponds to a Git commit blob.
    """

    def __init__(self, repos, rev):
        self.log = repos.log

        if type(rev) is pygit2.Commit:
            commit = rev
            rev = commit.hex
        else:
            commit = repos._resolve_rev(rev)
            rev = commit.hex

        author = repos._get_commit_username(commit)
        date = repos._get_commit_time(commit)

        self.commit = commit
        Changeset.__init__(self, repos, rev, commit.message, author, date)

    def get_branches(self):
        return self.repos._get_branches_cset(self.rev)

    def get_tags(self):
        return self.repos._get_tags_cset(self.rev)

    def get_properties(self):
        properties = {}
        commit = self.commit

        if commit.parents:
            properties['git-Parents'] = [c.hex for c in commit.parents]

        if (commit.author.name != commit.committer.name or
            commit.author.email != commit.committer.email):
            properties['git-committer'] = commit.committer
            properties['git-author'] = commit.author

        branches = self.repos._get_branches(self.rev)
        if branches:
            properties['git-Branches'] = branches
        tags = self.get_tags()
        if tags:
            properties['git-Tags'] = tags

        children = self.repos.child_revs(self.rev)
        if children:
            properties['git-Children'] = children

        return properties

    def get_changes(self):
        commit = self.commit
        if commit.parents:
            # diff for the first parent if even merge-commit
            parent = commit.parents[0]
            parent_rev = parent.hex
            files = self.repos._get_changes(parent.tree, commit.tree)
        else:
            _from_fspath = self.repos._from_fspath
            files = sorted(((None, _from_fspath(name), 'A')
                            for git_object, name in _walk_tree(
                                        self.repos.git_repos, commit.tree)),
                           key=lambda change: change[1])
            parent_rev = None

        for old_path, new_path, status in files:
            action = _status_map.get(status)
            if not action:
                continue
            if status == 'A':
                yield new_path, Node.FILE, action, None, None
            else:
                yield new_path, Node.FILE, action, old_path, parent_rev
