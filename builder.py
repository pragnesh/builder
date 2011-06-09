#!/usr/bin/env python
import datetime, json, optparse, os, os.path, subprocess, sys, time, webbrowser
from stat import S_IMODE
import BaseHTTPServer, tempfile, threading, urlparse

import boto, paramiko
from boto.ec2.autoscale import AutoScalingGroup, LaunchConfiguration, Trigger
from boto.s3.key import Key
from boto.cloudfront import CloudFrontConnection

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
	""" Print error and exit """
	sys.exit('%s %s' % (alert('\nerror:'), message))

def warning(message):
	""" Print warning """
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
	""" Call ssh with a host, key, and a command to run """
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

def load_balance(ec2, env):
	""" Create the load balancers if they do not exist """
	elb = boto.connect_elb(ec2.access_key, ec2.secret_key)
	for machine in env:
		if 'load_balancer' in machine.keys():
			lb_list = [lb.name for lb in elb.get_all_load_balancers()]
			load_balancer = machine['load_balancer']
			
			# Set the defaults
			availability_zones = machine.get('availability_zones',['us-east-1a', 'us-east-1c', 'us-east-1d'])
			lb_name            = load_balancer.get('name',{})
			lb_listeners       = load_balancer.get('listeners',[(80, 80, 'http')])
			health_check       = load_balancer.get('health_check',{})

			# Create a health check for the load balancer
			hc = boto.ec2.elb.HealthCheck(
					health_check.get('name','instance_health'),
					interval            = health_check.get('interval', 20),
					target              = health_check.get('target', 'HTTP:80/'),
					healthy_threshold   = health_check.get('healthy_threshold',2),
					timeout             = health_check.get('timeout',5),
					unhealthy_threshold = health_check.get('unhealthy_threshold',5),
					)

			# Create the load balancer if it does not exist
			if lb_name not in lb_list:
				new_lb = elb.create_load_balancer(lb_name, availability_zones, lb_listeners)
				new_lb.configure_health_check(hc)
				print 'Creating load balancer ', new_lb
				machine['load_balancer']['host'] = new_lb.dns_name

def autoscale(ec2, env):
	""" Autoscale each machine """
	asg = boto.connect_autoscale(ec2.access_key, ec2.secret_key)
	for machine in env:
		if 'autoscale' in machine.keys():
			print 'Autoscaling %s' % machine['name']
			autoscale = machine['autoscale']

			# Set the defaults
			autoscale_name     = autoscale.get('name','_'.join(machine['name'].split()))
			launch_config_name = autoscale.get('launch_config_name','launch_config_%s' % autoscale_name)
			group_name         = autoscale.get('group_name','group_%s' % autoscale_name)
			trigger_name       = autoscale.get('trigger_name','trigger_%s' % autoscale_name)
			min_size           = autoscale.get('min_size','1')
			max_size           = autoscale.get('max_size','4')

			availability_zones = machine.get('availability_zones',['us-east-1a', 'us-east-1c', 'us-east-1d'])
			load_balancers     = [machine.get('load_balancer',{}).get('name')]

			# Create ec2 launch configuration
			lc = LaunchConfiguration(      
			            name            = launch_config_name,           
			            image_id        = machine['image'],     
			            key_name        = machine['key_pair'],
			            instance_type   = machine['size'],   
			            security_groups = machine['groups'])
			asg.create_launch_configuration(lc)

			# Create ec2 autoscaling group 
			ag = AutoScalingGroup(
			            group_name         = group_name, 
			            load_balancers     = load_balancers,
			            availability_zones = availability_zones,
			            launch_config      = lc,
			            min_size           = min_size,
			            max_size           = max_size)
			asg.create_auto_scaling_group(ag)
			
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
			    'breach_duration'        : '120',
			    'lower_threshold'        : '15',
			    'upper_threshold'        : '30',
			    'lower_breach_scale_increment' : '-1',
			    'upper_breach_scale_increment' : '2', 

			}
			trigger_config.update(autoscale.get('trigger_config',{}))
			tr = Trigger(**trigger_config)
			asg.create_trigger(tr)

def s3_percent_cb(complete, total):
	""" Callback method for s3 bucket """
	sys.stdout.write('.')
	sys.stdout.flush()

def s3bucket(ec2, env, source):
	""" Copy contents of static directory to s3 bucket """
	mime_types = {
		"eot" : "application/vnd.ms-fontobject",
		"ttf" : "font/truetype",
		"otf" : "font/opentype",
		"woff": "font/woff",
	}
	s3b = boto.connect_s3(ec2.access_key,ec2.secret_key)
	for machine in env:
		if 's3bucket' in machine.keys():
			print 'Copying static media for %s' % machine['name']
			s3bucket = machine['s3bucket']

			# Get the expires
			time_format = '%a, %d %b %Y %H:%M:%S'
			now = datetime.datetime.now().strftime(time_format)
			expires = s3bucket.get('expires',datetime.datetime.utcnow().strftime(time_format))
			try:
				datetime.datetime.strptime(expires,time_format)
			except:
				error('Improperly formatted datetime: %s' % expires)

			# Get or create bucket using the name
			name    = s3bucket.get('name','s3%s'%machine['name'])
			try: b = s3b.get_bucket(name)
			except: b = s3b.create_bucket(name)
			
			# Set ACL Public for all items in the bucket
			b.set_acl('public-read')

			k = Key(b)
			static_dir = os.path.join(source,'project','static')
			for root, dirs, files in os.walk(static_dir):
				if '.svn' in dirs: dirs.remove('.svn')
				key_root = root.split('static')[1]

				for file in files:
					filename = os.path.join(root,file)

					# Set the headers
					headers = {'Expires':expires}
					if '.gz' in file:
						headers.update({'Content-Encoding':'gzip'})

					if os.path.isfile(filename):
						# Set the mime-type
						ext = file.split('.')[-1]
						if ext in mime_types.keys():
							k.content_type = mime_types[ext]

						# Send the file
						k.key = os.path.join(key_root,file)
						print '\nTransfering %s' % filename
						k.set_contents_from_filename(filename, headers=headers, cb=s3_percent_cb, num_cb=10)
			print '\nTransfer complete'

def invalidate_cache(ec2, env, source):
	""" Invalidate CloudFront cache for each machine with a Cloudfront Distribution ID"""
	# NOTE: Creating distributions is not yet supported, only cache invalidation
	cfc = CloudFrontConnection(ec2.access_key,ec2.secret_key)
	for machine in env:
		if 'cloudfront' in machine.keys():
			print 'Invalidating cache for %s' % machine['name']
			cloudfront = machine['cloudfront'] # Cloudfront Distribution ID

			media_files = []
			static_dir = os.path.join(source,'project','static')
			for root, dirs, files in os.walk(static_dir):
				if '.svn' in dirs: dirs.remove('.svn')
				key_root = root.split('static')[1]

				for file in files:
					filename = os.path.join(root,file)
					if os.path.isfile(filename):
						media_files.append(os.path.join(key_root,file))

			cfc.create_invalidation_request(cloudfront, media_files)

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
		<ul>%(servers)s</ul>
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
		servers = ''
		kwargs = {'actions':'', 'status':self.server.status, 'version':VERSION}
		for e in self.server.settings['deploy']:
			servers += '<li>%s<ul>'%e
			for m in self.server.settings['deploy'][e]:
				h = m.get('host', '')
				servers += ('<li><a href="%s">%s</a></li>' % (
					h and 'http://%s%s'%(h, m.get('url', '')) or '',
					m.get('name', 'Unnamed Machine')))
			servers += '</ul></li>'
		kwargs['servers'] = servers
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
			updater = Background(update, self.server.reset,
					[self.server.ec2, env, source])
			if action == 'Build':
				self.server.status = 'building'
				Background(build, updater.start,
						[self.server.ec2, env, source]).start()
			elif action == 'Update':
				self.server.status = 'updating'
				updater.start()

def get_map(ec2):
	""" Map the data from each available connection """
	# Get extra connections
	elb = boto.connect_elb(ec2.access_key, ec2.secret_key)
	asg = boto.connect_autoscale(ec2.access_key, ec2.secret_key)
	s3b = boto.connect_s3(ec2.access_key,ec2.secret_key)

	# EC2 Keypairs
	keys = {}
	for k in ec2.get_all_key_pairs():
		keys[k.name] = k.fingerprint
	
	# EC2 Security Groups
	security_groups = {}
	for s in ec2.get_all_security_groups():
		rules = {}
		for r in s.rules:
			g = str(r.grants)
			if g not in rules: rules[g] = []
			rules[g].append('%s:[%s%s]' % (r.ip_protocol, r.from_port,
				r.to_port != r.from_port and '-'+r.to_port or ''))
		security_groups[s.name] = rules
	
	# Elastic Load Balancers
	elbs = {}
	for lb in elb.get_all_load_balancers():
		info = {}
		info['instances'] = lb.instances
		info['dns_name']  = lb.dns_name
		elbs[lb.name] = info

	# Need to map out 'asg'
	# * Launch Configurations
	# * AutoScaling Groups
	# * AutoScaling Triggers and Instances

	# S3 Buckets
	buckets = {}
	for b in s3b.get_all_buckets():
		buckets[b.name] = b

	# EC2 Instances
	instances = {}
	for r in ec2.get_all_instances():
		for i in r.instances:
			if i.image_id not in instances:
				instances[i.image_id] = {}
			if i.state not in instances[i.image_id]:
				instances[i.image_id][i.state] = []
			instances[i.image_id][i.state].append(i)
	
	data = {
		'asgs': {},
		'elbs': elbs,
		'instances': instances,
		'keys': keys,
		's3bs': buckets,
		'security_groups': security_groups,
	}
	return data

def print_map(ec2):
	data = get_map(ec2)
	keys = data.get('keys')
	if keys:
		print 'Key Pairs:'
		for k, v in keys.iteritems():
			print '\t', k, '\t', v

	security_groups = data.get('security_groups')
	if security_groups:
		print
		print 'Security Groups:'
		for k, v in security_groups.iteritems():
			print '\t', k
			for k2, v2 in v.iteritems():
				print '\t\t', k2
				for g in v2: print '\t\t\t', g

	elbs = data.get('elbs')
	if elbs:
		print
		print 'Elastic Load Balancers:'
		for k, v in elbs.iteritems():
			print '\t', k
			for k2, v2 in v.iteritems():
				print '\t\t', k2, '\t', v2

	buckets = data.get('buckets')
	if buckets:
		print
		print 'Buckets:'
		for k, v in buckets.iteritems():
			print '\t', k, '\t', v

	instances = data.get('instances')
	if instances:
		print
		print 'Instances:'
		for k, v in instances.iteritems():
			print '\tAMI: %s (%s)' % (k, 'running' in v and 
					', '.join([g.groupName for g in v['running'][0].groups])
					or 'no images running')
			for k2, v2 in v.iteritems():
				print '\t\t%s:' % k2, ', '.join([k2=='running' and
					i.public_dns_name or i.reason for i in v2])

def main(options):
	# Get or create the conf file and set the settings
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

	# Open EC2 connection
	ec2 = boto.connect_ec2(settings['key'], settings['secret'])

	# Create the server
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
	
	# Create the key
	if options.key:
		cwd = os.getcwd()
		ec2.create_key_pair(options.key).save(cwd)
		print 'Generated %s' % path(os.path.join(cwd, '%s.pem'%options.key))
	
	# Print a map of the data
	if options.map: print_map(ec2)

	# Open the shell
	if options.shell:
		sys.argv = sys.argv[:1]
		try:
			from IPython.Shell import IPShellEmbed
			IPShellEmbed()(local_ns=locals())
		except:
			import code
			code.interact()
		return
	
	# Set the source and env variables from the config
	source = prepare(settings, dir=options.dir, tag=options.tag)
	env = settings['deploy'].get(options.env, None)

	# Build or Update
	if options.build or options.update:
		if not env: error('deploy %s not found' % options.env)

		# Build and update
		if options.build:
			n = 1 #TODO: Calculate number of new servers
			res = raw_input('Create %s server%s [y/N]? ' % (n, n>1 and 's' or ''))
			if res and res.lower()[0] == 'y':
				build(ec2, env, source)
			else: print "Not building servers"
		update(ec2, env, source)
		json.dump(settings, open(conf, 'w'), indent=4)
	
		# Load Balance Machines and Autoscale Machines
		load_balance(ec2, env)
		autoscale(ec2, env)

		# Clean up after autoscaling
		for machine in env:
			print 'autoscale' in machine, 'load_balancer' in machine
			if 'autoscale' in machine and 'load_balancer' in machine:
				get_instance(ec2, machine['host']).terminate()
				env['host'] = env['load_balancer']['host']
		json.dump(settings, open(conf, 'w'), indent=4)

	# Push static media to s3bucket
	if options.bucket:
		s3bucket(ec2,env,source)
	
	# Invalidate cloudfront cache
	if options.cache:
		invalidate_cache(ec2,env,source)

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
	parser.add_option('-S', '--s3bucket', action='store_true',
			dest='bucket', help='upload static files to s3bucket',)
	parser.add_option('-C', '--cache_invalidate', action='store_true',
			dest='cache', help='invalidate cloudfront cache',)
	parser.add_option('-T', '--template',
			help='use template file FILE to build out new config',
			metavar='FILE')
	(kwargs, args) = parser.parse_args()
	main(kwargs)
