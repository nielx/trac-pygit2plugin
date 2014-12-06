#!/usr/bin/env python
# -*- coding: utf-8 -*-


def main():
    from setuptools import setup, find_packages

    kwargs = {
        'name': 'TracPygit2Plugin',
        'version': '0.12.0.1',
        'description': 'Pygit2 integration for Git repository on Trac 0.12+',
        'license': 'BSD',  # the same as Trac
        'url': 'http://trac-hacks.org/wiki/TracPygit2Plugin',
        'author': 'Jun Omae',
        'author_email': 'jun66j5@gmail.com',
        'packages': find_packages(exclude=['*.tests*']),
        'package_data': {
            'tracext.pygit2': ['locale/*/LC_MESSAGES/*.mo'],
        },
        'test_suite': 'tracext.pygit2.tests.suite',
        'zip_safe': False,
        'install_requires': ['Trac', 'pygit2'],
        'entry_points': {
            'trac.plugins': [
                'tracext.pygit2.git_fs = tracext.pygit2.git_fs',
                'tracext.pygit2.translation = tracext.pygit2.translation',
            ],
        },
    }
    try:
        import babel
        from trac.util.dist import get_l10n_cmdclass
    except ImportError:
        pass
    else:
        kwargs['message_extractors'] = {
            'tracext': [
                ('**.py', 'python', None),
                ('**.html', 'genshi', None),
            ],
        }
        kwargs['cmdclass'] = get_l10n_cmdclass()

    setup(**kwargs)


if __name__ == '__main__':
    main()
