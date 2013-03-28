from setuptools import setup

VERSION = '0.1.2'
PACKAGE = 'ticketreminder'

setup(
	name = 'TicketReminderPlugin',
	version = VERSION,
	description = "Allows to configure reminders for tickets in Trac.",
	author = 'Mitar',
	author_email = 'mitar.trac@tnode.com',
	url = 'http://mitar.tnode.com/',
	keywords = 'trac plugin',
	license = "AGPLv3",
	packages = [PACKAGE],
    include_package_data = True,
    package_data = {
        PACKAGE: [
            'htdocs/css/*.css',
            'htdocs/js/*.js',
            'templates/*.html',
        ],
    },
	install_requires = [],
	zip_safe = False,
	entry_points = {
		'trac.plugins': '%s = %s' % (PACKAGE, PACKAGE),
	},
)
