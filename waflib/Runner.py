#!/usr/bin/env python
# encoding: utf-8
# Thomas Nagy, 2005-2017 (ita)

"""
Runner.py: Task scheduling and execution
"""

import random
try:
	from queue import Queue
except ImportError:
	from Queue import Queue
from waflib import Utils, Task, Errors, Logs

GAP = 20
"""
Wait for at least ``GAP * njobs`` before trying to enqueue more tasks to run
"""

class Consumer(Utils.threading.Thread):
	"""
	Daemon thread object that executes a task. It shares a semaphore with
	the coordinator :py:class:`waflib.Runner.Spawner`. There is one
	instance per task to consume.
	"""
	def __init__(self, spawner, task):
		Utils.threading.Thread.__init__(self)
		self.task = task
		"""Task to execute"""
		self.spawner = spawner
		"""Coordinator object"""
		self.setDaemon(1)
		self.start()
	def run(self):
		"""
		Processes a single task
		"""
		try:
			if not self.spawner.master.stop:
				self.task.process()
		finally:
			self.spawner.sem.release()
			self.spawner.master.out.put(self.task)
			self.task = None
			self.spawner = None

class Spawner(Utils.threading.Thread):
	"""
	Daemon thread that consumes tasks from :py:class:`waflib.Runner.Parallel` producer and
	spawns a consuming thread :py:class:`waflib.Runner.Consumer` for each
	:py:class:`waflib.Task.Task` instance.
	"""
	def __init__(self, master):
		Utils.threading.Thread.__init__(self)
		self.master = master
		""":py:class:`waflib.Runner.Parallel` producer instance"""
		self.sem = Utils.threading.Semaphore(master.numjobs)
		"""Bounded semaphore that prevents spawning more than *n* concurrent consumers"""
		self.setDaemon(1)
		self.start()
	def run(self):
		"""
		Spawns new consumers to execute tasks by delegating to :py:meth:`waflib.Runner.Spawner.loop`
		"""
		try:
			self.loop()
		except Exception:
			# Python 2 prints unnecessary messages when shutting down
			# we also want to stop the thread properly
			pass
	def loop(self):
		"""
		Consumes task objects from the producer; ends when the producer has no more
		task to provide.
		"""
		master = self.master
		while 1:
			task = master.ready.get()
			self.sem.acquire()
			if not master.stop:
				task.log_display(task.generator.bld)
			Consumer(self, task)

class Parallel(object):
	"""
	Schedule the tasks obtained from the build context for execution.
	"""
	def __init__(self, bld, j=2):
		"""
		The initialization requires a build context reference
		for computing the total number of jobs.
		"""

		self.numjobs = j
		"""
		Amount of parallel consumers to use
		"""

		self.bld = bld
		"""
		Instance of :py:class:`waflib.Build.BuildContext`
		"""

		self.outstanding = Utils.deque()
		"""List of :py:class:`waflib.Task.Task` that may be ready to be executed"""

		self.frozen = set()
		"""Set of :py:class:`waflib.Task.Task` that need other tasks to complete first"""

		self.incomplete = Utils.deque()
		"""List of :py:class:`waflib.Task.Task` with incomplete dependencies"""

		self.ready = Queue(0)
		"""List of :py:class:`waflib.Task.Task` ready to be executed by consumers"""

		self.out = Queue(0)
		"""List of :py:class:`waflib.Task.Task` returned by the task consumers"""

		self.count = 0
		"""Amount of tasks that may be processed by :py:class:`waflib.Runner.TaskConsumer`"""

		self.processed = 1
		"""Amount of tasks processed"""

		self.stop = False
		"""Error flag to stop the build"""

		self.error = []
		"""Tasks that could not be executed"""

		self.biter = None
		"""Task iterator which must give groups of parallelizable tasks when calling ``next()``"""

		self.dirty = False
		"""
		Flag that indicates that the build cache must be saved when a task was executed
		(calls :py:meth:`waflib.Build.BuildContext.store`)"""

		self.revdeps = Utils.defaultdict(set)
		"""
		The reverse dependency graph of dependencies obtained from Task.run_after
		"""

		self.spawner = Spawner(self)
		"""
		Coordinating daemon thread that spawns thread consumers
		"""

	def get_next_task(self):
		"""
		Obtains the next Task instance to run

		:rtype: :py:class:`waflib.Task.Task`
		"""
		if not self.outstanding:
			return None
		return self.outstanding.popleft()

	def postpone(self, tsk):
		"""
		Adds the task to the list :py:attr:`waflib.Runner.Parallel.incomplete`.
		The order is scrambled so as to consume as many tasks in parallel as possible.

		:param tsk: task instance
		:type tsk: :py:class:`waflib.Task.Task`
		"""
		if random.randint(0, 1):
			self.incomplete.appendleft(tsk)
		else:
			self.incomplete.append(tsk)

	def refill_task_list(self):
		"""
		Adds the next group of tasks to execute in :py:attr:`waflib.Runner.Parallel.outstanding`.
		"""
		while self.count > self.numjobs * GAP:
			self.get_out()

		while not self.outstanding:
			if self.count:
				self.get_out()
			elif self.incomplete:
				cond = self.deadlock == self.processed
				if cond:
					msg = 'check the build order for the tasks'
					for tsk in self.incomplete:
						if not tsk.run_after:
							msg = 'check the methods runnable_status'
							break
					lst = []
					for tsk in self.incomplete:
						lst.append('%s\t-> %r' % (repr(tsk), [id(x) for x in tsk.run_after]))
					raise Errors.WafError('Deadlock detected: %s%s' % (msg, ''.join(lst)))

			if self.incomplete:
				self.outstanding.extend(self.incomplete)
				self.incomplete.clear()
			elif not self.count:
				tasks = next(self.biter)
				ready, waiting = self.prio_and_split(tasks)
				# We cannot use a priority queue because the implementation
				# must be able to handle incomplete dependencies
				self.outstanding.extend(ready)
				self.frozen.update(waiting)
				self.total = self.bld.total()
				break

	def insert_with_prio(self, tsk):
		# TODO the deque interface has insert in python 3.5 :-/
		if self.outstanding and tsk.prio >= self.outstanding[0].prio:
			self.outstanding.appendleft(tsk)
		else:
			self.outstanding.append(tsk)

	def add_more_tasks(self, tsk):
		"""
		If a task provides :py:attr:`waflib.Task.Task.more_tasks`, then the tasks contained
		in that list are added to the current build and will be processed before the next build group.

		Assume that the task is done, so that task priorities do not need
		to be re-calculated

		:param tsk: task instance
		:type tsk: :py:attr:`waflib.Task.Task`
		"""
		if getattr(tsk, 'more_tasks', None):
			ready, waiting = self.prio_and_split(tsk.tasks)
			for k in ready:
				# TODO could be better, but we will have 1 task in general?
				self.insert_with_prio(k)
			self.frozen.update(waiting)
			self.total += len(tsk.more_tasks)

	def mark_finished(self, tsk):
		# we assume that frozen tasks will be consumed as the build goes

		def try_unfreeze(x):
			# DAG ancestors are likely to be frozen
			if x in self.frozen:
				# TODO remove dependencies to free some memory?
				# x.run_after.remove(tsk)
				for k in x.run_after:
					if not k.hasrun:
						break
				else:
					self.frozen.remove(x)
					self.insert_with_prio(x)

		if tsk in self.revdeps:
			for x in self.revdeps[tsk]:
				if isinstance(x, Task.TaskGroup):
					x.prev.remove(tsk)
					if not x.prev:
						for k in x.next:
							# TODO necessary optimization?
							k.run_after.remove(x)
							try_unfreeze(k)
						# TODO necessary optimization?
						x.next = []
				else:
					try_unfreeze(x)
			del self.revdeps[tsk]

	def get_out(self):
		"""
		Waits for a Task that task consumers add to :py:attr:`waflib.Runner.Parallel.out` after execution.
		Adds more Tasks if necessary through :py:attr:`waflib.Runner.Parallel.add_more_tasks`.

		:rtype: :py:attr:`waflib.Task.Task`
		"""
		tsk = self.out.get()
		if not self.stop:
			self.add_more_tasks(tsk)
		self.mark_finished(tsk)

		self.count -= 1
		self.dirty = True
		return tsk

	def add_task(self, tsk):
		"""
		Enqueue a Task to :py:attr:`waflib.Runner.Parallel.ready` so that consumers can run them.

		:param tsk: task instance
		:type tsk: :py:attr:`waflib.Task.Task`
		"""
		self.ready.put(tsk)

	def skip(self, tsk):
		"""
		Mark a task as skipped/up-to-date
		"""
		tsk.hasrun = Task.SKIPPED
		self.mark_finished(tsk)

	def cancel(self, tsk):
		"""
		Mark a task as failed because of unsatisfiable dependencies
		"""
		tsk.hasrun = Task.CANCELED
		self.mark_finished(tsk)

	def error_handler(self, tsk):
		"""
		Called when a task cannot be executed. The flag :py:attr:`waflib.Runner.Parallel.stop` is set, unless
		the build is executed with::

			$ waf build -k

		:param tsk: task instance
		:type tsk: :py:attr:`waflib.Task.Task`
		"""
		if hasattr(tsk, 'scan') and hasattr(tsk, 'uid'):
			# TODO waf 2.0 - this breaks encapsulation
			try:
				del self.bld.imp_sigs[tsk.uid()]
			except KeyError:
				pass
		if not self.bld.keep:
			self.stop = True
		self.error.append(tsk)

	def task_status(self, tsk):
		"""
		Obtains the task status to decide whether to run it immediately or not.

		:return: the exit status, for example :py:attr:`waflib.Task.ASK_LATER`
		:rtype: integer
		"""
		try:
			return tsk.runnable_status()
		except Exception:
			self.processed += 1
			tsk.err_msg = Utils.ex_stack()
			if not self.stop and self.bld.keep:
				self.skip(tsk)
				if self.bld.keep == 1:
					# if -k stop on the first exception, if -kk try to go as far as possible
					if Logs.verbose > 1 or not self.error:
						self.error.append(tsk)
					self.stop = True
				else:
					if Logs.verbose > 1:
						self.error.append(tsk)
				return Task.EXCEPTION
			tsk.hasrun = Task.EXCEPTION

			self.error_handler(tsk)
			return Task.EXCEPTION

	def start(self):
		"""
		Obtains Task instances from the BuildContext instance and adds the ones that need to be executed to
		:py:class:`waflib.Runner.Parallel.ready` so that the :py:class:`waflib.Runner.Spawner` consumer thread
		has them executed. Obtains the executed Tasks back from :py:class:`waflib.Runner.Parallel.out`
		and marks the build as failed by setting the ``stop`` flag.
		If only one job is used, then executes the tasks one by one, without consumers.
		"""
		self.total = self.bld.total()

		while not self.stop:

			self.refill_task_list()

			# consider the next task
			tsk = self.get_next_task()
			if not tsk:
				if self.count:
					# tasks may add new ones after they are run
					continue
				else:
					# no tasks to run, no tasks running, time to exit
					break

			if tsk.hasrun:
				# if the task is marked as "run", just skip it
				self.processed += 1
				continue

			if self.stop: # stop immediately after a failure is detected
				break

			st = self.task_status(tsk)
			if st == Task.RUN_ME:
				self.count += 1
				self.processed += 1

				if self.numjobs == 1:
					tsk.log_display(tsk.generator.bld)
					try:
						tsk.process()
					finally:
						self.out.put(tsk)
				else:
					self.add_task(tsk)
			elif st == Task.ASK_LATER:
				self.postpone(tsk)
			elif st == Task.SKIP_ME:
				self.processed += 1
				self.skip(tsk)
				self.add_more_tasks(tsk)
			elif st == Task.CANCEL_ME:
				# A dependency problem has occured, and the
				# build is most likely run with `waf -k`
				if Logs.verbose > 1:
					self.error.append(tsk)
				self.processed += 1
				self.cancel(tsk)

		# self.count represents the tasks that have been made available to the consumer threads
		# collect all the tasks after an error else the message may be incomplete
		while self.error and self.count:
			self.get_out()

		self.ready.put(None)
		assert (self.count == 0 or self.stop)

	def prio_and_split(self, tasks):
		"""
		Label input tasks with priority values, and return a pair containing
		the tasks that are ready to run and the tasks that are necessarily
		waiting for other tasks to complete.

		The priority system is really meant as an optional layer for optimization:
		dependency cycles are found more quickly, and build should be more efficient

		:return: A pair of task lists
		:rtype: tuple
		"""
		# to disable:
		#return tasks, []
		for x in tasks:
			x.visited = 0

		reverse = self.revdeps

		for x in tasks:
			for k in x.run_after:
				if isinstance(k, Task.TaskGroup):
					if k.done:
						pass
					else:
						k.done = True
						for j in k.prev:
							reverse[j].add(k)
				else:
					reverse[k].add(x)

		# the priority number is not the tree size
		def visit(n):
			if isinstance(n, Task.TaskGroup):
				return sum(visit(k) for k in n.next)

			if n.visited == 0:
				n.visited = 1
				if n in reverse:
					rev = reverse[n]
					n.prio = n.priority() + len(rev) + sum(visit(k) for k in rev)
				else:
					n.prio = n.priority()
				n.visited = 2
			elif n.visited == 1:
				raise Errors.WafError('Dependency cycle found!')
			return n.prio

		for x in tasks:
			if x.visited != 0:
				# must visit all to detect cycles
				continue
			try:
				visit(x)
			except Errors.WafError:
				self.debug_cycles(tasks, reverse)

		ready = []
		waiting = []
		for x in tasks:
			for k in x.run_after:
				if not k.hasrun:
					waiting.append(x)
					break
			else:
				ready.append(x)

		ready.sort(key=lambda x: x.prio, reverse=True)
		return (ready, waiting)

	def debug_cycles(self, tasks, reverse):
		# TODO display more than one cycle?
		tmp = {}
		for x in tasks:
			tmp[x] = 0

		def visit(n, acc):
			if isinstance(n, Task.TaskGroup):
				for k in n.next:
					visit(k)
			if tmp[n] == 0:
				tmp[n] = 1
				for k in reverse.get(n, []):
					visit(k, [n] + acc)
				tmp[n] = 2
			elif tmp[n] == 1:
				lst = []
				for tsk in acc:
					lst.append(repr(tsk))
					if tsk is n:
						# exclude prior nodes, we want the minimum cycle
						break
				raise Errors.WafError('Task dependency cycle in "run_after" constraints: %s' % ''.join(lst))
		for x in tasks:
			visit(x, [])

