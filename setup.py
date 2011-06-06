from distutils.core import setup

setup(
	name         = 'builder',
	packages     = ['builder',],
	version      = 'v0.0.1',
	author       = 'RED Interactive Agency',
	author_email = 'geeks@ff0000.com',

	url          = 'http://www.github.com/ff0000/builder/',

	license      = 'MIT license',
	description  = """ A tool to build and deploy onto Amazon EC2 servers """,

	long_description = open('README.markdown').read(),

	classifiers  = (
		'Development Status :: 3 - Alpha',
		'Environment :: Web Environment',
		'Intended Audience :: Developers',
		'License :: OSI Approved :: MIT License',
		'Programming Language :: Python',
	),
)
