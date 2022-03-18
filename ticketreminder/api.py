import re
import os.path

from pkg_resources import resource_filename

from trac.core import *
from trac.config import *
from trac.test import MockPerm
from trac.admin import IAdminCommandProvider
from trac.attachment import AttachmentModule
from trac.db import DatabaseManager
from trac.env import IEnvironmentSetupParticipant, Environment
from trac.web import IRequestHandler, IRequestFilter
from trac.web.chrome import (ITemplateProvider, add_stylesheet, add_link, 
                             add_ctxtnav, INavigationContributor, add_warning, 
                             add_script, add_script_data, Chrome, add_notice,
                             web_context)
from trac.web.main import FakeSession
from trac.web.api import Request
from trac.wiki import format_to_oneliner
from trac.util import get_reporter_id, lazy
from trac.util.html import tag, Markup
from trac.util.datefmt import (pretty_timedelta, to_datetime, format_date, 
                               get_date_format_hint, format_datetime, 
                               parse_date, _time_intervals, to_utimestamp, 
                               datetime_now, utc, get_timezone, localtz)
from trac.util.text import (exception_to_unicode, text_width, CRLF, wrap, 
                            jinja2template, to_unicode)
from trac.util.translation import _, deactivate, make_activable, reactivate, tag_
from trac.ticket import Ticket, ITicketChangeListener
from trac.ticket.api import translation_deactivated
from trac.ticket.web_ui import TicketModule
from trac.ticket.notification import TicketFormatter, TicketChangeEvent
from trac.timeline.web_ui import TimelineModule
from trac.perm import IPermissionRequestor, PermissionError, PermissionSystem
from trac.resource import get_resource_url, get_resource_name
from trac.notification.api import (NotificationSystem, INotificationSubscriber, 
                                   NotificationEvent, IEmailDecorator, 
                                   INotificationFormatter)
from trac.notification.mail import (RecipientMatcher, create_message_id,
                                    get_from_author, get_message_addresses,
                                    set_header)
from trac.notification.model import Subscription

try:
    from babel.core import Locale
except ImportError:
    Locale = None

if Locale:
    def _parse_locale(lang):
        try:
            return Locale.parse(lang, sep='-')
        except:
            return Locale('en', 'US')
else:
    _parse_locale = lambda lang: None

import ticketreminder.db_default as db_default


class TicketReminder(Component):
    """
    With this component you can configure reminders for tickets in Trac.
    """

    implements(IEnvironmentSetupParticipant, 
               ITemplateProvider, IRequestHandler, IRequestFilter,
               INavigationContributor, IPermissionRequestor,
               ITicketChangeListener, IAdminCommandProvider,
               INotificationFormatter, IEmailDecorator)

    # IEnvironmentSetupParticipant methods

    def environment_created(self):
        """Called when a new Trac environment is created."""

        self.found_db_version = 0
        self.upgrade_environment()

    def environment_needs_upgrade(self, db=None):
        """Called when Trac checks whether the environment needs to be
        upgraded.
        """
        value = self.env.db_query("""
            SELECT value FROM system WHERE name=%s
            """, (db_default.name,))
        if not value:
            self.found_db_version = 0
            return True
        else:
            self.found_db_version = int(value[0][0])
            if self.found_db_version < db_default.version:
                return True

        return False

    def upgrade_environment(self, db=None):
        """Actually perform an environment upgrade."""

        connector = DatabaseManager(self.env).get_connector()[0]

        with self.env.db_transaction as db:
            for table in db_default.schema:
                for stmt in connector.to_sql(table):
                    db(stmt)

            if not self.found_db_version:
                db("""
                    INSERT INTO system (name, value) VALUES (%s, %s)
                    """, (db_default.name, db_default.version))
            else:
                db("""
                    UPDATE system SET value=%s WHERE name=%s
                    """, (db_default.version, db_default.name))

        self.log.info('Upgraded %s schema version from %d to %d',
                      db_default.name, self.found_db_version,
                      db_default.version)

    # IRequestFilter methods

    def pre_process_request(self, req, handler):
        if self.match_request(req):
            return self
        else:
            return handler

    def post_process_request(self, req, template, data, metadata):
        if template in ('ticket.html', 'ticket_preview.html'):
            if not self.match_request(req):
                if ('TICKET_REMINDER_VIEW' in req.perm or
                     'TICKET_REMINDER_MODIFY' in req.perm or
                     'TICKET_ADMIN' in req.perm):
                    tags = self._reminder_tags(req, data)
                    if tags:
                        add_stylesheet(req, 'ticketreminder/css/ticketreminder.css')
                        add_script(req, 'ticketreminder/js/ticketreminderjinja.js')
                        add_script_data(req, trdata={'tags': str(tags)})
        return template, data, metadata

    # IRequestHandler methods

    def match_request(self, req):
        """Return whether the handler wants to process the given request."""

        match = re.match(r'/ticket/([0-9]+)$', req.path_info)
        if match and \
                req.args.get('action') in ("addreminder", "deletereminder"):
            req.args['id'] = match.group(1)
            return True

        return False

    def process_request(self, req):
        """Process the request."""

        id = int(req.args.get('id'))

        req.perm('ticket', id).require('TICKET_VIEW')

        if 'TICKET_REMINDER_MODIFY' not in req.perm and \
                'TICKET_ADMIN' not in req.perm:
            raise PermissionError('TICKET_REMINDER_MODIFY',
                                  req.perm._resource, self.env)

        ticket = Ticket(self.env, id)

        if 'cancel' in req.args:
            req.redirect(get_resource_url(self.env, ticket.resource, req.href))

        ticket_name = get_resource_name(self.env, ticket.resource)
        ticket_url = get_resource_url(self.env, ticket.resource, req.href)
        add_link(req, 'up', ticket_url, ticket_name)
        add_ctxtnav(req, _('Back to %(ticket)s', ticket=ticket_name),
                    ticket_url)

        add_stylesheet(req, 'ticketreminder/css/ticketreminder.css')

        if req.args['action'] == "addreminder":
            return self._process_add(req, ticket)
        elif req.args['action'] == "deletereminder":
            return self._process_delete(req, ticket)
        else:
            raise ValueError('Unknown action "%s"' % (req.args['action'],))

    def _process_add(self, req, ticket):
        if req.method == "POST" and self._validate_add(req):
            if req.args.get('reminder_type') == 'interval':
                time = clear_time(to_datetime(None))
                delta = _time_intervals[req.args.get('unit')](req.args.get('interval'))
                time += delta
                time = to_utimestamp(time)
            else:
                time = to_utimestamp(parse_date(req.args.get('date')))
            origin = to_utimestamp(to_datetime(None))

            self.env.db_transaction("""
                INSERT INTO ticketreminder
                 (ticket, time, author, origin, reminded, description)
                VALUES (%s, %s, %s, %s, 0, %s)
                """, (ticket.id, time, get_reporter_id(req, 'author'),
                      origin, req.args.get('description')))

            add_notice(req, "Reminder has been added.")
            req.redirect(get_resource_url(self.env, ticket.resource, req.href) + "#reminders")

        Chrome(self.env).add_jquery_ui(req)
        add_script(req, 'ticketreminder/js/ticketreminder.js')

        data = {
            'ticket': ticket,
            'date_hint': get_date_format_hint(),
        }

        return "ticket_reminder_add_jinja.html", data

    def _validate_add(self, req):
        ty = req.args.get('reminder_type')

        if ty == 'interval':
            try:
                req.args['interval'] = int(req.args.get('interval', '').strip())
                if req.args['interval'] <= 0:
                    add_warning(req, "Nonpositive interval value.")
                    return False
            except ValueError:
                add_warning(req, "Invalid or missing interval value.")
                return False

            if req.args.get('unit') not in ['day', 'week', 'month', 'year']:
                add_warning(req, "Please select interval unit.")
                return False

        elif ty == 'date':
            try:
                time = clear_time(parse_date(req.args.get('date', '').strip()))
                req.args['date'] = format_date(time)
                now = to_datetime(None)
                if time <= now:
                    add_warning(req, "Date value not in the future.")
                    return False
            except TracError:
                add_warning(req, "Invalid or missing date value.")
                return False

        else:
            add_warning(req, "Please select type.")
            return False

        return True

    def _process_delete(self, req, ticket):
        reminder_id = req.args.get('reminder')
        redirect_url = get_resource_url(self.env, ticket.resource, req.href)

        with self.env.db_transaction as db:
            for reminder in db("""
                    SELECT id, time, author, origin, description
                    FROM ticketreminder WHERE id=%s
                    """, (reminder_id,)):
                break
            else:
                add_warning(req, "Could not find reminder to delete.")
                req.redirect(redirect_url)
            if req.method == "POST":
                db("""
                    DELETE FROM ticketreminder WHERE id=%s
                    """, (reminder_id,))

        if req.method == "POST":
            add_notice(req, "Reminder has been deleted.")
            req.redirect(redirect_url + "#reminders")

        kwargs = {'delete_button': False}
        data = {
            'ticket': ticket,
            'formatted_reminder':
                self._format_reminder(req, ticket, *reminder, **kwargs),
        }

        return "ticket_reminder_delete_jinja.html", data

    def _get_reminders(self, ticket_id):
        for row in self.env.db_query("""
            SELECT id, time, author, origin, description
            FROM ticketreminder
            WHERE ticket=%s AND reminded=0 ORDER BY time
            """, (ticket_id,)):
            yield row

    def _format_reminder(self, req, ticket, id, time, author, origin, description, delete_button=True):
        now = to_datetime(None)
        time = to_datetime(time)
        if now >= time:
            when = tag(tag.strong("Right now"), " (pending)")
        else:
            when = tag("In ", tag.strong(pretty_timedelta(time)), " (", format_date(time), ")")

        if description:
            context = web_context(req, ticket.resource)
            desc = tag.div(format_to_oneliner(self.env, context, description), class_="description")
        else:
            desc = tag()

        return tag(self._reminder_delete_form(req, id) if delete_button else None, when, " - added by ", tag.em(Chrome(self.env).authorinfo(req, author)), " ", tag.span(pretty_timedelta(origin), title=format_datetime(origin, req.session.get('datefmt', 'iso8601'), req.tz)), " ago.", desc)

    def _format_reminder_text(self, ticket, id, author, origin, description):
        return "Ticket reminder added by %s %s ago (%s)%s" % (author, pretty_timedelta(origin), format_datetime(origin), ":\n%s" % (description,) if description else ".")

    def _reminder_tags(self, req, data):
        if 'ticket' not in data or not data['ticket'].id:
            return None

        ticket = data['ticket']

        if ticket['status'] == 'closed':
            return None

        li_tags = [tag.li(self._format_reminder(req, ticket, *args)) for args in self._get_reminders(ticket.id)]
        if li_tags:
            list_tags = tag.ul(li_tags, class_="reminders")
        else:
            list_tags = []

        add_form = self._reminder_add_form(req)

        if not list_tags and not add_form:
            return None

        return \
            tag.div(
                tag.h3("Reminders", tag.span("(" + str(len(li_tags)) + ")", class_='trac-count'), class_="foldable"),
                tag.div(
                    list_tags,
                    add_form,
                ),
                id="reminders",
            )

    def _reminder_trac_nav(self, req, data):
        return tag(Markup(' <a href="#reminders" title="Go to the list of reminders">Reminders</a> &uarr;'))

    def _reminder_add_form(self, req):
        if 'TICKET_REMINDER_MODIFY' not in req.perm and 'TICKET_ADMIN' not in req.perm:
            return None

        return \
            tag.form(
                tag.div(
                    tag.input(type="hidden", name="action", value="addreminder"),
                    tag.input(type="submit", value="Add reminder"),
                ),
                method="get",
                action="",
                id="addreminder",
            )

    def _reminder_delete_form(self, req, reminder_id):
        if 'TICKET_REMINDER_MODIFY' not in req.perm and 'TICKET_ADMIN' not in req.perm:
            return None

        return \
            tag.form(
                tag.div(
                    tag.input(type="hidden", name="action", value="deletereminder"),
                    tag.input(type="hidden", name="reminder", value=reminder_id),
                    tag.input(type="submit", value="Delete"),
                    class_="inlinebuttons",
                ),
                method="get",
                action="",
            )

    # ITemplateProvider methods

    def get_htdocs_dirs(self):
        yield 'ticketreminder', resource_filename(__name__, 'htdocs')

    def get_templates_dirs(self):
        yield resource_filename(__name__, 'templates')

    # INavigationContributor methods

    def get_active_navigation_item(self, req):
        return 'tickets'

    def get_navigation_items(self, req):
        return []

    # IPermissionRequestor methods

    def get_permission_actions(self):
        return ['TICKET_REMINDER_VIEW', 'TICKET_REMINDER_MODIFY']

    # ITicketChangeListener methods

    def ticket_created(self, ticket):
        pass

    def ticket_changed(self, ticket, comment, author, old_values):
        pass

    def ticket_deleted(self, ticket):
        self.env.db_transaction("""
            DELETE FROM ticketreminder WHERE ticket=%s
            """, (ticket.id,))

    # IAdminCommandProvider methods

    def get_admin_commands(self):
        yield ('reminders', '', 'Check for any pending reminders and send them', None, self._do_check_and_send)

    def _do_check_and_send(self):
        now = to_utimestamp(to_datetime(None))
        for row in self.env.db_query("""
                SELECT id, ticket, author, origin, description FROM ticketreminder WHERE reminded=0 AND %s>=time
                    """, (now,)):
            self._do_send(*row)

    def _do_send(self, id, ticket, author, origin, description):
        ticket = Ticket(self.env, ticket)
        try:
            # We send reminder only for open tickets
            if ticket['status'] != 'closed':
                reminder = self._format_reminder_text(ticket, id, author, origin, description)
                now = datetime_now(utc)
                event = TicketReminderEvent('reminder', ticket, now, author, reminder)
                notifier = NotificationSystem(self.env)
                notifier.notify(event)
        except Exception as e:
            self.env.log.error("Failure sending reminder notification for ticket #%s: %s", ticket.id, exception_to_unicode(e))
            print("Failure sending reminder notification for ticket #%s: %s" % (ticket.id, exception_to_unicode(e)))
        else:
            # We set flag anyway as even for closed tickets this notification
            # would be obsolete if ticket would be reopened
            self.env.db_transaction("""
                UPDATE ticketreminder SET reminded=1 WHERE id=%s
                """, (id,))

    # INotificationFormatter methods

    COLS = 75

    ambiguous_char_width = Option('notification', 'ambiguous_char_width',
                                  'single',
        """Width of ambiguous characters that should be used in the table
        of the notification mail.

        If `single`, the same width as characters in US-ASCII. This is
        expected by most users. If `double`, twice the width of
        US-ASCII characters.  This is expected by CJK users.
        """)

    ticket_subject_template = Option('notification', 'ticket_subject_template',
                                     '${prefix} #${ticket.id}: ${summary}',
        """A Jinja2 text template snippet used to get the notification
        subject.

        The template variables are documented on the
        [TracNotification#Customizingthee-mailsubject TracNotification] page.
        """)

    @lazy
    def ambiwidth(self):
        return 2 if self.ambiguous_char_width == 'double' else 1

    def get_supported_styles(self, transport):
        return (('text/plain', 'ticketreminder'), ('text/html', 'ticketreminder'))

    def format(self, transport, style, event):
        if event.realm != 'ticketreminder':
            return
        if style == 'text/html':
            #raise ValueError("This is a test of the plaintext formatting.")
            return self._format_html(event)
        else:
            return self._format_plaintext(event)

    def _format_html(self, event):
        chrome = Chrome(self.env)
        req = self._create_request()
        ticket = event.target
        cnum = None
        if event.time:
            rows = self._db_query("""\
                SELECT field, oldvalue FROM ticket_change
                WHERE ticket=%s AND time=%s AND field='comment'
                """, (ticket.id, to_utimestamp(event.time)))
            for field, oldvalue in rows:
                if oldvalue:
                    cnum = int(oldvalue.rsplit('.', 1)[-1])
                    break
        link = self.env.abs_href.ticket(ticket.id)
        if cnum is not None:
            link += '#comment:%d' % cnum

        try:
            tx = deactivate()
            try:
                make_activable(lambda: req.locale, self.env.path)
                content = self._create_html_body(chrome, req, ticket, cnum,
                                                 link, event)
            finally:
                reactivate(tx)
        except:
            self.log.warn('Caught exception while generating html part',
                          exc_info=True)
            raise
        try:
            if isinstance(content, unicode):
                # avoid UnicodeEncodeError from MIMEText()
                content = content.encode('utf-8')
        except NameError:
            if isinstance(content, str):
                # avoid UnicodeEncodeError from MIMEText()
                content = content.encode('utf-8')
        return content

    if hasattr(Environment, 'db_query'):
        def _db_query(self, query, args=()):
            return self.env.db_query(query, args)
    else:
        def _db_query(self, query, args=()):
            db = self.env.get_read_db()
            cursor = db.cursor()
            cursor.execute(query, args)
            return list(cursor)

    def _create_request(self):
        languages = list(filter(None, [self.config.get('trac', 'default_language')]))
        if languages:
            locale = _parse_locale(languages[0])
        else:
            locale = None
        tzname = self.config.get('trac', 'default_timezone')
        tz = get_timezone(tzname) or localtz
        base_url = self.env.abs_href()
        if ':' in base_url:
            url_scheme = base_url.split(':', 1)[0]
        else:
            url_scheme = 'http'
        environ = {'REQUEST_METHOD': 'POST', 'REMOTE_ADDR': '127.0.0.1',
                   'SERVER_NAME': 'localhost', 'SERVER_PORT': '80',
                   'wsgi.url_scheme': url_scheme, 'trac.base_url': base_url}
        if languages:
            environ['HTTP_ACCEPT_LANGUAGE'] = ','.join(languages)
        session = FakeSession()
        session['dateinfo'] = 'absolute'
        req = Request(environ, lambda *args, **kwargs: None)
        req.arg_list = ()
        req.args = {}
        req.authname = 'anonymous'
        req.form_token = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        req.session = session
        req.perm = MockPerm()
        req.href = req.abs_href
        req.locale = locale
        req.lc_time = locale
        req.tz = tz
        req.chrome = {'notices': [], 'warnings': []}
        return req

    def _create_html_body(self, chrome, req, ticket, cnum, link, event):
        tktmod = TicketModule(self.env)
        data = tktmod._prepare_data(req, ticket)
        tktmod._insert_ticket_data(req, ticket, data, req.authname, {})
        data['ticket']['link'] = link
        context = web_context(req, ticket.resource, absurls=True)
        data.update({'can_append': False,
                     'show_editor': False,
                     'start_time': ticket['changetime'],
                     'context': context,
                     'styles': self._get_styles(chrome),
                     'link': tag.a(link, href=link),
                     'tag_': tag_})
        data['reminder'] = event.reminder
        template = 'ticket_reminder_email_jinja.html'
        # use pretty_dateinfo in TimelineModule
        TimelineModule(self.env).post_process_request(req, template, data,
                                                      None)
        return chrome.render_template(req, template, data,
                                          {'iterable': False})

    def _get_styles(self, chrome):
        # Added search for 'shared' to get configured css
        for provider in chrome.template_providers:
            for prefix, dir in provider.get_htdocs_dirs():
                if prefix != 'shared':
                    continue
                url_re = re.compile(r'\burl\([^\]]*\)')
                buf = ['#content > hr { display: none }']
                for name in ('trac.css', 'ticket.css'):
                    f = open(os.path.join(dir, 'css', name))
                    try:
                        lines = f.read().splitlines()
                    finally:
                        f.close()
                    buf.extend(url_re.sub('none', to_unicode(line))
                               for line in lines
                               if not line.startswith('@import'))
                return ('/*<![CDATA[*/\n' +
                        '\n'.join(buf).replace(']]>', ']]]]><![CDATA[>') +
                        '\n/*]]>*/')

        for provider in chrome.template_providers:
            for prefix, dir in provider.get_htdocs_dirs():
                if prefix != 'common':
                    continue
                url_re = re.compile(r'\burl\([^\]]*\)')
                buf = ['#content > hr { display: none }']
                for name in ('trac.css', 'ticket.css'):
                    f = open(os.path.join(dir, 'css', name))
                    try:
                        lines = f.read().splitlines()
                    finally:
                        f.close()
                    buf.extend(url_re.sub('none', to_unicode(line))
                               for line in lines
                               if not line.startswith('@import'))
                return ('/*<![CDATA[*/\n' +
                        '\n'.join(buf).replace(']]>', ']]]]><![CDATA[>') +
                        '\n/*]]>*/')
        return ''

    def _format_plaintext(self, event):
        """Format ticket reminder e-mail (untranslated)"""
        ticket = event.target
        with translation_deactivated(ticket):
            link = self.env.abs_href.ticket(ticket.id)

            changes_body = ''
            changes_descr = ''
            change_data = {}

            ticket_values = ticket.values.copy()
            ticket_values['id'] = ticket.id
            ticket_values['description'] = wrap(
                ticket_values.get('description', ''), self.COLS,
                initial_indent=' ', subsequent_indent=' ', linesep='\n',
                ambiwidth=self.ambiwidth)
            ticket_values['new'] = False
            ticket_values['link'] = link

            data = Chrome(self.env).populate_data(None, {
                'CRLF': CRLF,
                'reminder': event.reminder,
                'ticket_props': self._format_props(ticket),
                'ticket_body_hdr': self._format_hdr(ticket),
                'ticket': ticket_values,
                'changes_body': changes_body,
                'changes_descr': changes_descr,
                'change': change_data
            })
            return self._format_body(data, 'ticket_reminder_email_jinja.txt')

    def _format_author(self, author):
        return Chrome(self.env).format_author(None, author)

    def _format_body(self, data, template_name):
        chrome = Chrome(self.env)
        template = chrome.load_template(template_name, text=True)
        with translation_deactivated():  # don't translate the e-mail stream
            body = chrome.render_template_string(template, data, text=True)
            return body.encode('utf-8')

    def _format_subj(self, event):
        ticket = event.target

        summary = ticket['summary']

        prefix = self.config.get('notification', 'smtp_subject_prefix')
        if prefix == '__default__':
            prefix = '[%s]' % self.env.project_name

        data = {
            'prefix': prefix,
            'summary': summary,
            'ticket': ticket,
            'changes': None,
            'env': self.env,
        }

        template = _template_from_string(self.ticket_subject_template)
        subj = template.render(**data).strip()
        subj = "Ticket Reminder: " + subj
        return subj

    def _format_hdr(self, ticket):
        return '#%s: %s' % (ticket.id, wrap(ticket['summary'], self.COLS,
                                            linesep='\n',
                                            ambiwidth=self.ambiwidth))

    def _format_props(self, ticket):
        fields = [f for f in ticket.fields
                  if f['name'] not in ('summary', 'cc', 'time', 'changetime')]
        width = [0, 0, 0, 0]
        i = 0
        for f in fields:
            if f['type'] == 'textarea':
                continue
            fname = f['name']
            if fname not in ticket.values:
                continue
            fval = ticket[fname] or ''
            if fname in ticket.time_fields:
                format = ticket.fields.by_name(fname).get('format')
                fval = self._format_time_field(fval, format)
            if fval.find('\n') != -1:
                continue
            if fname in ['owner', 'reporter']:
                fval = self._format_author(fval)
            flabel = f['label']
            idx = 2 * (i % 2)
            width[idx] = max(self._get_text_width(flabel), width[idx])
            width[idx + 1] = max(self._get_text_width(fval), width[idx + 1])
            i += 1
        width_l = width[0] + width[1] + 5
        width_r = width[2] + width[3] + 5
        half_cols = (self.COLS - 1) // 2
        if width_l + width_r + 1 > self.COLS:
            if ((width_l > half_cols and width_r > half_cols) or
                    (width[0] > half_cols // 2 or width[2] > half_cols // 2)):
                width_l = half_cols
                width_r = half_cols
            elif width_l > width_r:
                width_l = min((self.COLS - 1) * 2 // 3, width_l)
                width_r = self.COLS - width_l - 1
            else:
                width_r = min((self.COLS - 1) * 2 // 3, width_r)
                width_l = self.COLS - width_r - 1
        sep = width_l * '-' + '+' + width_r * '-'
        txt = sep + '\n'
        vals_lr = ([], [])
        big = []
        i = 0
        width_lr = [width_l, width_r]
        for f in [f for f in fields if f['name'] != 'description']:
            fname = f['name']
            if fname not in ticket.values:
                continue
            fval = ticket[fname] or ''
            if fname in ticket.time_fields:
                format = ticket.fields.by_name(fname).get('format')
                fval = self._format_time_field(fval, format)
            if fname in ['owner', 'reporter']:
                fval = self._format_author(fval)
            flabel = f['label']
            if f['type'] == 'textarea' or '\n' in str(fval):
                big.append((flabel, '\n'.join(fval.splitlines())))
            else:
                # Note: flabel is a Babel's LazyObject, make sure its
                # __str__ method won't be called.
                str_tmp = '%s:  %s' % (flabel, str(fval))
                idx = i % 2
                initial_indent = ' ' * (width[2 * idx] -
                                        self._get_text_width(flabel) +
                                        2 * idx)
                wrapped = wrap(str_tmp, width_lr[idx] - 2 + 2 * idx,
                               initial_indent, '  ', '\n', self.ambiwidth)
                vals_lr[idx].append(wrapped.splitlines())
                i += 1
        if len(vals_lr[0]) > len(vals_lr[1]):
            vals_lr[1].append([])

        cell_l = []
        cell_r = []
        for i in range(len(vals_lr[0])):
            vals_l = vals_lr[0][i]
            vals_r = vals_lr[1][i]
            vals_diff = len(vals_l) - len(vals_r)
            diff = len(cell_l) - len(cell_r)
            if diff > 0:
                # add padding to right side if needed
                if vals_diff < 0:
                    diff += vals_diff
                cell_r.extend([''] * max(diff, 0))
            elif diff < 0:
                # add padding to left side if needed
                if vals_diff > 0:
                    diff += vals_diff
                cell_l.extend([''] * max(-diff, 0))
            cell_l.extend(vals_l)
            cell_r.extend(vals_r)

        for i in range(max(len(cell_l), len(cell_r))):
            if i >= len(cell_l):
                cell_l.append(width_l * ' ')
            elif i >= len(cell_r):
                cell_r.append('')
            fmt_width = width_l - self._get_text_width(cell_l[i]) \
                        + len(cell_l[i])
            txt += '%-*s|%s%s' % (fmt_width, cell_l[i], cell_r[i], '\n')
        if big:
            txt += sep
            for name, value in big:
                txt += '\n'.join(['', name + ':', value, '', ''])
        txt += sep
        return txt

    def _format_time_field(self, value, format):
        tzinfo = get_timezone(self.config.get('trac', 'default_timezone'))
        return format_date_or_datetime(format, value, tzinfo=tzinfo) \
               if value else ''

    def _get_text_width(self, text):
        return text_width(text, ambiwidth=self.ambiwidth)

    # IEmailDecorator methods

    def _get_from_email(self, event):
        from_email = get_from_author(self.env, event)
        if from_email and isinstance(from_email, tuple):
            from_email = from_email[1]
        if not from_email:
            from_email = self.config.get('notification', 'smtp_from') or \
                         self.config.get('notification', 'smtp_replyto')
        return from_email

    def _get_message_id(self, targetid, from_email, modtime, more=None):
        return create_message_id(self.env, targetid, from_email, modtime, more)

    def decorate_message(self, event, message, charset):
        if event.realm != 'ticketreminder':
            return
        from_email = self._get_from_email(event)
        subject = self._format_subj(event)
        ticket = event.target
        targetid = '%08d' % ticket.id
        more = ticket['reporter'] or ''
        msgid = self._get_message_id(targetid, from_email, None, more)
        url = self.env.abs_href.ticket(ticket.id)
        if event.category != 'created':
            set_header(message, 'In-Reply-To', msgid, charset)
            set_header(message, 'References', msgid, charset)
            msgid = self._get_message_id(targetid, from_email, event.time,
                                         more)
            cnum = ticket.get_comment_number(event.time)
            if cnum is not None:
                url += '#comment:%d' % cnum
        set_header(message, 'X-Trac-Ticket-ID', ticket.id, charset)
        set_header(message, 'X-Trac-Ticket-URL', url, charset)
        # When owner, reporter and updater are listed in the Cc header,
        # move the address to To header.
        if NotificationSystem(self.env).use_public_cc:
            to_addrs = set()
            matcher = RecipientMatcher(self.env)
            for rcpt in ticket['owner'], ticket['reporter'], event.author:
                rcpt = matcher.match_recipient(rcpt)
                if not rcpt:
                    continue
                addr = rcpt[2]
                if addr:
                    to_addrs.add(addr)
            if to_addrs:
                cc_addrs = get_message_addresses(message, 'Cc')
                to_addrs &= set(addr for name, addr in cc_addrs)
            if to_addrs:
                cc_header = ', '.join(create_header('Cc', (name, addr),
                                                    charset)
                                      for name, addr in cc_addrs
                                      if addr not in to_addrs)
                if cc_header:
                    set_header(message, 'Cc', cc_header, charset)
                elif 'Cc' in message:
                    del message['Cc']
                to_header = ', '.join(sorted(to_addrs))
                set_header(message, 'To', to_header, charset)
        set_header(message, 'Subject', subject, charset)
        set_header(message, 'Message-ID', msgid, charset)



class TicketReminderTicketReminderAuthorSubscriber(Component):
    """Allows ticket reminder creators to subscribe to reminders they created."""

    implements(INotificationSubscriber)

    def matches(self, event):
        authors = None
        if (event.realm == 'ticketreminder') and ('reminder' in event.category):
            authors = [event.author]
        return _ticket_reminder_subscribers(self, authors)

    def description(self):
        return "Ticket for which I created a reminder has the reminder"

    def default_subscriptions(self):
        klass = self.__class__.__name__
        return NotificationSystem(self.env).default_subscriptions(klass)

    def requires_authentication(self):
        return True


class TicketReminderTicketOwnerSubscriber(Component):
    """Allows ticket owners to subscribe to reminders for their tickets."""

    implements(INotificationSubscriber)

    def matches(self, event):
        owners = None
        if (event.realm == 'ticketreminder') and ('reminder' in event.category):
            owners = [event.target['owner']]
        return _ticket_reminder_subscribers(self, owners)

    def description(self):
        return "Ticket that I own has a reminder"

    def default_subscriptions(self):
        klass = self.__class__.__name__
        return NotificationSystem(self.env).default_subscriptions(klass)

    def requires_authentication(self):
        return True


class TicketReminderCarbonCopySubscriber(Component):
    """Carbon copy subscriber reminders for cc ticket field."""

    implements(INotificationSubscriber)

    def matches(self, event):
        cc_users = None
        if (event.realm == 'ticketreminder') and ('reminder' in event.category):
            # CC field is stored as comma-separated string. Parse to set.
            chrome = Chrome(self.env)
            to_set = lambda cc: set(chrome.cc_list(cc))
            cc_users = to_set(event.target['cc'] or '')
        return _ticket_reminder_subscribers(self, cc_users)

    def description(self):
        return "Ticket that I'm listed in the CC field has a reminder"

    def default_subscriptions(self):
        klass = self.__class__.__name__
        return NotificationSystem(self.env).default_subscriptions(klass)

    def requires_authentication(self):
        return True


class TicketReminderTicketReporterSubscriber(Component):
    """Allows the users to subscribe to reminders for tickets that they report."""

    implements(INotificationSubscriber)

    def matches(self, event):
        reporter = None
        if (event.realm == 'ticketreminder') and ('reminder' in event.category):
            reporter = event.target['reporter']
        return _ticket_reminder_subscribers(self, reporter)

    def description(self):
        return "Ticket that I reported has a reminder"

    def default_subscriptions(self):
        klass = self.__class__.__name__
        return NotificationSystem(self.env).default_subscriptions(klass)

    def requires_authentication(self):
        return True


class TicketReminderTicketPreviousUpdatersSubscriber(Component):
    """Allows subscribing to reminders simply by updating a ticket."""

    implements(INotificationSubscriber)

    def matches(self, event):
        updaters = None
        if (event.realm == 'ticketreminder') and ('reminder' in event.category):
            updaters = [author for author, in self.env.db_query("""
                SELECT DISTINCT author FROM ticket_change
                WHERE ticket=%s
                """, (event.target.id,))]
        return _ticket_reminder_subscribers(self, updaters)

    def description(self):
        return "Ticket that I previously updated has a reminder"

    def default_subscriptions(self):
        klass = self.__class__.__name__
        return NotificationSystem(self.env).default_subscriptions(klass)

    def requires_authentication(self):
        return True


class TicketReminderEvent(NotificationEvent):
    """Represent a ticket reminder `NotificationEvent`."""

    def __init__(self, category, target, time, author, reminder):
        super(TicketReminderEvent, self).__init__('ticketreminder', category, target,
                                                  time, author)
        self.reminder = reminder
        self.comment = None
        self.changes = {}
        self.attachment = None


def _ticket_reminder_subscribers(subscriber, candidates):
    if not candidates:
        return
    if not isinstance(candidates, (list, set, tuple)):
        candidates = [candidates]

    # Get members of permission groups
    groups = PermissionSystem(subscriber.env).get_groups_dict()
    for cc in set(candidates):
        if cc in groups:
            candidates.remove(cc)
            candidates.update(groups[cc])

    matcher = RecipientMatcher(subscriber.env)
    klass = subscriber.__class__.__name__
    sids = set()
    for candidate in candidates:
        recipient = matcher.match_recipient(candidate)
        if not recipient:
            continue
        sid, auth, addr = recipient

        # Default subscription
        for s in subscriber.default_subscriptions():
            yield s[0], s[1], sid, auth, addr, s[2], s[3], s[4]
        if sid:
            sids.add((sid, auth))

    for s in Subscription.find_by_sids_and_class(subscriber.env, sids, klass):
        yield s.subscription_tuple()


def _template_from_string(string):
    return jinja2template(string, text=True, line_statement_prefix=None,
                          line_comment_prefix=None)


def clear_time(date):
    return date.replace(hour=0, minute=0, second=0, microsecond=0)
