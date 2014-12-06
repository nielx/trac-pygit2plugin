# -*- coding: utf-8 -*-

from trac.config import BoolOption, IntOption, Option
from trac.core import Component, implements
from trac.env import IEnvironmentSetupParticipant
from trac.util.translation import dgettext, domain_functions


def domain_options(domain, *options):
    import inspect
    if 'doc_domain' in inspect.getargspec(Option.__init__)[0]:
        def _option_with_tx(Option, doc_domain):  # Trac 1.0+
            def fn(*args, **kwargs):
                kwargs['doc_domain'] = doc_domain
                return Option(*args, **kwargs)
            return fn
    else:
        def _option_with_tx(Option, doc_domain):  # Trac 0.12.x
            class OptionTx(Option):
                def __getattribute__(self, name):
                    if name == '__class__':
                        return Option
                    value = Option.__getattribute__(self, name)
                    if name == '__doc__':
                        value = dgettext(doc_domain, value)
                    return value
            return OptionTx
    return map(lambda option: _option_with_tx(option, domain), options)


TEXTDOMAIN = 'tracpygit2'
BoolOption, IntOption, Option = domain_options(
    TEXTDOMAIN, BoolOption, IntOption, Option)
N_, _, add_domain, gettext, tag_ = domain_functions(
    TEXTDOMAIN, 'N_', '_', 'add_domain', 'gettext', 'tag_')


class TracPygit2Translation(Component):

    implements(IEnvironmentSetupParticipant)

    def __init__(self):
        from pkg_resources import resource_filename
        try:
            locale_dir = resource_filename(__name__, 'locale')
        except KeyError:
            pass
        else:
            add_domain(self.env.path, locale_dir)

    # IEnvironmentSetupParticipant methods

    def environment_created(self):
        pass

    def environment_needs_upgrade(self, db):
        return False

    def upgrade_environment(self, db):
        pass
