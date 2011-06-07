#!/usr/bin/env python
import datetime, json, optparse, os, os.path, subprocess, sys, time, webbrowser
from stat import S_IMODE
import BaseHTTPServer, tempfile, threading, urlparse
import boto, paramiko

default_ami      = 'ami-1aad5273' #64-bit Ubuntu 11.04, us-east-1
default_key_pair = 'ec2.example'

# Globals
VERSION  = '0.0.1'
alert    = lambda s: '\033[31m%s\033[0m' % s
path     = lambda s: '\033[36m%s\033[0m' % s
defaults = {'key':None, 'secret':None, 'repo':None, 'deploy':{
	'default':[{'base':default_ami, 'size':'t1.micro', 'groups':['default'],
		'key_pair':default_key_pair, 'name':'example', 'init':[], 'update':[],
		'url':'/'
	}],
}}
def error(message):
	sys.exit('%s %s' % (alert('\nerror:'), message))

def warning(message):
	print alert('warning:'), message

def get_key(source, name):
	""" 
	Return the path to the key file given a source directory and keyname.
	
	This method will always assume that the key exists in the deploy folder
	of the source directory.

	This method will always ensure that the key has proper permissions for SSH.
	"""

	key = os.path.join(source, 'deploy', '%s.pem' % name)
	if not os.path.exists(key):
		error('key [%s] not found at %s, aborting' % (name, path(key)))
	mode = oct(S_IMODE(os.stat(key).st_mode))
	if mode not in ['0600','0400']:
		error('key [%s] with perms %s must have 0600 or 0400, aborting' % (name,mode))
	return key

def ssh(host, key, command):
	client = paramiko.SSHClient()
	client.load_system_host_keys()
	client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
	client.connect(host, username='ubuntu', key_filename=key) #TODO:Username?
	i, o, e = client.exec_command(command)
	o, e = o.read(), e.read()
	if e: warning(e)
	return o

#def ssh(host, key, command):
#	subprocess.call('ssh -i %s ubuntu@%s "%s"' % (key, host, command), shell=True)

def prepare(settings, dir=None, tag=None):
	if dir: source = os.path.abspath(dir)
	else:
		source = tempfile.mkdtemp(prefix='builder.%s.' %
				settings['repo'].split('/')[-2])
		if tag not in ('trunk', ''): tag = 'tags/%s' % tag
		subprocess.call('svn co %s%s %s' %
				(settings['repo'], tag, source), shell=True)
	return source

def get_instance(ec2, hostname):
	""" Return an ec2 instance given a hostname or return None """
	for reservation in ec2.get_all_instances(filters={'dns-name':hostname}):
		for instance in reservation.instances:
			if instance.public_dns_name == hostname:
				return instance
	return None

def build(ec2, env, source):
	print 'Building servers'
	if isinstance(env, dict): env=[env]
	for machine in env:
		image = ec2.get_image(machine['base'])
		key   = get_key(source, machine['key_pair'])
		print 'Requesting %s' % machine['name']
		res = image.run(key_name=machine['key_pair'],
				security_groups=machine['groups'],
				instance_type=machine['size'])
		i = res.instances[0]
		while i.update() == 'pending':
			print 'Waiting ten seconds on %s' % i
			time.sleep(10)
		if 'host' in machine:
			warning('%s has been replaced' % machine['host'])
			#TODO: Terminate?  ec2.get_all_instances(filters={'dns-name':machine['host']})
		machine['host'] = i.public_dns_name
		i.add_tag('Name', machine['name'])
		while 1:
			try:
				print 'Seeing if %s is actually online' % machine['host']
				ssh(machine['host'], key, 'echo "hi!"')
				break
			except:
				print 'Nope, trying again in five seconds'
				time.sleep(5)
		for command in machine['init']:
			print 'Running [%s]' % command
			ssh(machine['host'], key, command)

def update(ec2, env, source):
	print 'Updating servers'
	for machine in env:
		key = get_key(source, machine['key_pair'])
		if 'host' not in machine: error('%s has no host entry' % machine['name'])
		target = '/srv/%s' % os.path.basename(source)
		ssh(machine['host'], key,
			'test -a %(path)s && mv %(path)s %(path)s.`date +%%m:%%d:%%H:%%M`' % {'path':target})
		print 'Deploying code'
		subprocess.call('rsync -aze "ssh -o StrictHostKeyChecking=no -i %s" %s ubuntu@%s:/srv' % (key, source, machine['host']), shell=True) #TODO:Username?
		ssh(machine['host'], key, 'rm /srv/active; ln -s %s /srv/active' % target)
		for command in machine['update']:
			print 'Running [%s]' % command
			ssh(machine['host'], key, command)
		if 'url' in machine:
			webbrowser.open('http://%s%s' % (machine['host'], machine['url']))

		# Image the updated instance
		instance = get_instance(ec2, machine['host'])
		now = datetime.datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
		machine['image'] = ec2.create_image(instance.id, '%s %s' % (machine['name'],now), 
				description='Image of %s on %s' % (machine['name'],now))

def autoscale(ec2, env):
	for machine in env:
		if 'autoscale' in machine.keys():
			print 'Autoscaling %s' % machine['name']
			autoscale = machine['autoscale']

			# Set defaults
			autoscale_name     = autoscale.get('name','_'.join(machine['name'].split()))
			launch_config_name = autoscale.get('launch_config_name','launch_config_%s' % autoscale_name)
			group_name         = autoscale.get('group_name','group_%s' % autoscale_name)
			trigger_name       = autoscale.get('trigger_name','trigger_%s' % autoscale_name)
			availability_zones = autoscale.get('availability_zones',['us-east-1a', 'us-east-1c', 'us-east-1d'])
			min_size           = autoscale.get('min_size','1')
			max_size           = autoscale.get('max_size','4')

			# Create ec2 launch configuration
			lc = boto.ec2.autoscale.LaunchConfiguration(      
			            name            = launch_config_name),           
			            image_id        = machine['image'],     
			            key_name        = machine['key_name']     
			            instance_type   = machine['size'],   
			            security_groups = machine['groups'])
			print lc
			ec2.create_launch_configuration(lc)
			
			# Create ec2 autoscaling group 
			ag = boto.ec2.autoscale.AutoScalingGroup(
			            group_name         = group_name, 
			            load_balancers     = machine['load_balancers'],
			            availability_zones = availability_zones,
			            launch_config      = lc,
			            min_size           = min_size,
			            max_size           = max_size)
			print ag
			ec2.create_auto_scaling_group(ag)
			
			# Create ec2 autoscaling group trigger
			trigger_config = {
				# Probably NOT a good idea to update these int he config
				'name'                   : trigger_name,
				'autoscale_group'        : ag,
			    'dimensions'             : [('AutoScalingGroupName', ag.name)],

				# These are fine to update in the config
			    'measure_name'           : 'CPUUtilization',             
			    'statistic'              : 'Average',
			    'unit'                   : 'Percent',
			    'period'                 : '60',
			    'breach-duration'        : '120',
			    'lower_threshold'        : '15',
			    'upper_threshold'        : '30',
			    'lower_breach_scale_increment' : '-1',
			    'upper_breach_scale_increment' : '2', 

			}
			trigger_config.update(autoscale.get('trigger_config',{}))
			tr = boto.ec2.autoscale.Trigger(trigger_config**)
			print tr
			ec2.create_trigger(tr)
	
			#trigger_config = {
			#    'region'                 : 'us-east-1',
			#    'auto-scaling-group'     : group_name,
			#    'measure'                : 'CPUUtilization',             
			#    'statistic'              : 'Average',
			#    'period'                 : '60',
			#    'lower-threshold'        : '15',
			#    'upper-threshold'        : '30',
			#    'lower-breach-increment' : '-1',
			#    'upper-breach-increment' : '2', 
			#    'breach-duration'        : '120',
			#    'unit'                   : 'Percent',
			#    'namespace'              : '"AWS/EC2"',
			#    'dimensions'             : '"AutoScalingGroupName=%s"' % group_name,
			#}
			#trigger = autoscale.get('trigger_config',{})
			#trigger_config.update(trigger)
			#command = ['as-create-or-update-trigger %s' % trigger_name]
			#command_args = ['--%s=%s' % (key,trigger_config[key]) for key in trigger_config]
			#command_line = ' '.join(command+command_args)
			#
			#args = shlex.split(command_line)
			#retcode = subprocess.call(args)

class Background(threading.Thread):
	def __init__(self, fn, finish=None, args=None, kwargs=None):
		self.fn = fn
		self.finish = finish
		self.args = args or []
		self.kwargs = kwargs or {}
		super(Background, self).__init__()
	def run(self):
		self.fn(*self.args, **self.kwargs)
		if self.finish: self.finish()

class BuildServer(BaseHTTPServer.BaseHTTPRequestHandler):
	html = '''<!doctype html><html>
	<head><title>Build Server %(version)s</title>
		<style type="text/css">
		.waiting {color:#0f0;}
		.building {color:#f00;}
		.updating {color:#00f;}
		.footer {font:x-small monospace; white-space:pre-wrap;}
		</style>
		<script type="text/javascript">
		window.onload = function(){
			var timer = document.getElementById('time');
			var time = 10;
			setInterval(function(){
				timer.innerText = --time;
				if (time < 1){
					window.location = window.location;
					time = 0;
				}
			}, 1000);
		}
		</script>
	</head>
	<body>
		<form method="POST">
		<div>Status: <span class="%(status)s">%(status)s</span></div>
		%(actions)s
		<div>Refreshing in <span id="time">10</span> seconds</div>
		</form>
		%(fortune)s
	</body>
	</html>
	'''
	actions = '''<input name="action" type="submit" value="Build" />
	<input name="action" type="submit" value="Update" />'''
	def do_GET(self):
		if self.path != '/':
			self.send_response(204)
			return
		self.send_response(200)
		self.send_header('Content-Type', 'text/html')
		self.end_headers()
		kwargs = {'actions':'', 'status':self.server.status, 'version':VERSION}
		try:
			kwargs['fortune'] = '<hr /><div class="footer">%s</div>' % subprocess.check_output('fortune')
		except: kwargs['fortune'] = ''
		if self.server.status == 'waiting':
			kwargs['actions'] = self.actions
		self.wfile.write(self.html % kwargs)
	def do_POST(self):
		self.send_response(301)
		self.send_header('Location', '/')
		self.end_headers()
		if self.server.status == 'waiting':
			post = urlparse.parse_qs(self.rfile.readline())
			action = post['action'][0]
			env = self.server.settings['deploy'][post['env'][0]]
			tag = self.server.tag #TODO: Make choosable?
			source = prepare(self.server.settings, dir=self.server.dir, tag=tag)
			if action == 'Build':
				self.server.status = 'building'
				Background(build, self.server.reset,
						[self.server.ec2, env, source]).start()
			elif action == 'Update':
				self.server.status = 'updating'
				Background(update, self.server.reset,
						[self.server.ec2, env, source]).start()

def map(ec2):
	keys = {}
	for k in ec2.get_all_key_pairs():
		keys[k.name] = k.fingerprint
	groups = {}
	for s in ec2.get_all_security_groups():
		rules = {}
		for r in s.rules:
			g = str(r.grants)
			if g not in rules: rules[g] = []
			rules[g].append('%s:[%s%s]' % (r.ip_protocol, r.from_port,
				r.to_port != r.from_port and '-'+r.to_port or ''))
		groups[s.name] = rules
	instances = {}
	for r in ec2.get_all_instances():
		for i in r.instances:
			if i.image_id not in instances:
				instances[i.image_id] = {}
			if i.state not in instances[i.image_id]:
				instances[i.image_id][i.state] = []
			instances[i.image_id][i.state].append(i)
	return keys, groups, instances

def main(options):
	conf = os.path.abspath(options.conf)
	if not os.path.exists(conf):
		if options.template:
			template = os.path.abspath(options.template)
			if os.path.exists(template):
				defaults.update(json.load(open(template,'r')))
		try:
			print path('%s' % conf) + ' not found, creating'
			while not defaults['key']:
				defaults['key'] = raw_input(' AWS Key: ')
			while not defaults['secret']:
				defaults['secret'] = raw_input('  Secret: ')
			defaults['repo'] = raw_input('SVN Repo: ')
			json.dump(defaults, open(conf, 'w'), sort_keys=True, indent=4)
			if 'EDITOR' in os.environ:
				subprocess.call('%s %s' % (os.environ['EDITOR'], conf), shell=True)
			if not defaults['repo']: warning('-t deployments will not work without a defined repo')
		except:
			error('conf file creation interrupted')
	settings = json.load(open(conf))
	ec2 = boto.connect_ec2(settings['key'], settings['secret'])
	if options.listen:
		def reset(self):
			self.status = 'waiting'
		server = BaseHTTPServer.HTTPServer(('', options.listen), BuildServer)
		server.dir = options.dir
		server.tag = options.tag
		server.settings = settings
		server.__class__.reset = reset #TODO: Use instance method?
		server.reset()
		server.ec2 = ec2
		BuildServer.actions = '<select name="env">%s</select> ' % ''.join(
				['<option value="%s">%s</option>' % (k,k)
					for k in settings['deploy']]) + BuildServer.actions
		server.serve_forever()
		return
	if options.key:
		cwd = os.getcwd()
		ec2.create_key_pair(options.key).save(cwd)
		print 'Generated %s' % path(os.path.join(cwd, '%s.pem'%options.key))
	if options.map:
		keys, groups, instances = map(ec2)
		print 'Key Pairs:'
		for k, v in keys.iteritems():
			print '\t', k, '\t', v
		print
		print 'Security Groups:'
		for k, v in groups.iteritems():
			print '\t', k
			for k2, v2 in v.iteritems():
				print '\t\t', k2
				for g in v2: print '\t\t\t', g
		print
		print 'Instances:'
		for k, v in instances.iteritems():
			print '\tAMI: %s (%s)' % (k, 'running' in v and 
					', '.join([g.groupName for g in v['running'][0].groups])
					or 'no images running')
			for k2, v2 in v.iteritems():
				print '\t\t%s:' % k2, ', '.join([k2=='running' and
					i.public_dns_name or i.reason for i in v2])
	if options.shell:
		sys.argv = sys.argv[:1]
		try:
			from IPython.Shell import IPShellEmbed
			IPShellEmbed()(local_ns=locals())
		except:
			import code
			code.interact()
		return
	if options.build or options.update:
		source = prepare(settings, dir=options.dir, tag=options.tag)
		env = settings['deploy'].get(options.env, None)
		if not env: error('deploy %s not found' % options.env)
		if options.build:
			n = 1 #TODO: Calculate number of new servers
			res = raw_input('Create %s server%s [y/N]? ' % (n, n>1 and 's' or ''))
			if res and res.lower()[0] == 'y':
				build(ec2, env, source)
			else: print "Not building servers"
		update(ec2, env, source)
		json.dump(settings, open(conf, 'w'), indent=4)

if __name__ == '__main__':
	# Command line parser
	parser  = optparse.OptionParser(version = '%%prog %s' % VERSION)
	parser.add_option('-m', '--map', action='store_true',
			dest='map', help='prints out ec2 information',)
	parser.add_option('-s', '--shell', action='store_true',
			dest='shell', help='spawn a shell in the current virtualenv',)
	parser.add_option('-b', '--build', action='store_true',
			dest='build', help='create new ec2 instances',)
	parser.add_option('-u', '--update', action='store_true',
			dest='update', help='update existing ec2 instances',)
	parser.add_option('-k', '--key', help='generate key KEY',
			metavar='KEY',)
	parser.add_option('-e', '--env', default='default',
			help='uses deploy ENV [default: %default]',
			metavar='ENV',)
	parser.add_option('-t', '--tag', default='trunk',
			help='uses tag TAG or trunk [default: %default]',
			metavar='TAG',)
	parser.add_option('-d', '--dir', help='uses dir DIR instead of --tag',
			metavar='DIR',)
	parser.add_option('-f', '--conf', default='./build.json',
			help='use config file FILE [default: %default]',
			metavar='FILE',)
	parser.add_option('-l', '--listen',
			help='listen for requests on port PORT',
			metavar='PORT', type='int')
	parser.add_option('-T', '--template',
			help='use template file FILE to build out new config',
			metavar='FILE')
	(kwargs, args) = parser.parse_args()
	main(kwargs)
