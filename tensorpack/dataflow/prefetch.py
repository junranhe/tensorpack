# -*- coding: UTF-8 -*-
# File: prefetch.py
# Author: Yuxin Wu <ppwwyyxx@gmail.com>

import multiprocessing
from threading import Thread
import itertools
from six.moves import range
from six.moves.queue import Queue
import uuid
import os

from .base import ProxyDataFlow
from ..utils.concurrency import *
from ..utils.serialize import *
from ..utils import logger

try:
    import zmq
except ImportError:
    logger.warn("Error in 'import zmq'. PrefetchDataZMQ won't be available.")
    __all__ = ['PrefetchData']
else:
    __all__ = ['PrefetchData', 'PrefetchDataZMQ']


class PrefetchProcess(multiprocessing.Process):
    def __init__(self, ds, queue):
        """
        :param ds: ds to take data from
        :param queue: output queue to put results in
        """
        super(PrefetchProcess, self).__init__()
        self.ds = ds
        self.queue = queue

    def run(self):
        # reset RNG of ds so each process will produce different data
        self.ds.reset_state()
        while True:
            for dp in self.ds.get_data():
                self.queue.put(dp)

class PrefetchData(ProxyDataFlow):
    """
    Prefetch data from a `DataFlow` using multiprocessing
    """
    def __init__(self, ds, nr_prefetch, nr_proc=1):
        """
        :param ds: a `DataFlow` instance.
        :param nr_prefetch: size of the queue to hold prefetched datapoints.
        :param nr_proc: number of processes to use. When larger than 1, order
            of data points will be random.
        """
        super(PrefetchData, self).__init__(ds)
        try:
            self._size = ds.size()
        except NotImplementedError:
            self._size = -1
        self.nr_proc = nr_proc
        self.nr_prefetch = nr_prefetch
        self.queue = multiprocessing.Queue(self.nr_prefetch)
        self.procs = [PrefetchProcess(self.ds, self.queue)
                      for _ in range(self.nr_proc)]
        ensure_proc_terminate(self.procs)
        for x in self.procs:
            x.start()

    def get_data(self):
        for k in itertools.count():
            if self._size > 0 and k >= self._size:
                break
            dp = self.queue.get()
            yield dp

class PrefetchProcessZMQ(multiprocessing.Process):
    def __init__(self, ds, conn_name):
        """
        :param ds: a `DataFlow` instance.
        :param conn_name: the name of the IPC connection
        """
        super(PrefetchProcessZMQ, self).__init__()
        self.ds = ds
        self.conn_name = conn_name

    def run(self):
        self.ds.reset_state()
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUSH)
        self.socket.set_hwm(1)
        self.socket.connect(self.conn_name)
        while True:
            for dp in self.ds.get_data():
                self.socket.send(dumps(dp), copy=False)

class PrefetchDataZMQ(ProxyDataFlow):
    """ Work the same as `PrefetchData`, but faster. """
    def __init__(self, ds, nr_proc=1, pipedir='.'):
        """
        :param ds: a `DataFlow` instance.
        :param nr_proc: number of processes to use. When larger than 1, order
            of datapoints will be random.
        :param pipedir: a local directory where the pipes would be. Useful if you're running on non-local FS such as NFS.
        """
        super(PrefetchDataZMQ, self).__init__(ds)
        try:
            self._size = ds.size()
        except NotImplementedError:
            self._size = -1
        self.nr_proc = nr_proc

        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PULL)
        assert os.path.isdir(pipedir)
        self.pipename = "ipc://{}/dataflow-pipe-".format(pipedir.rstrip('/')) + str(uuid.uuid1())[:6]
        self.socket.set_hwm(5)  # a little bit faster than default, don't know why
        self.socket.bind(self.pipename)

        self.procs = [PrefetchProcessZMQ(self.ds, self.pipename)
                      for _ in range(self.nr_proc)]
        start_proc_mask_signal(self.procs)

        # __del__ not guranteed to get called at exit
        import atexit
        atexit.register(lambda x: x.__del__(), self)

    def get_data(self):
        for k in itertools.count():
            if self._size > 0 and k >= self._size:
                break
            dp = loads(self.socket.recv(copy=False))
            yield dp

    def __del__(self):
        # on exit, logger may not be functional anymore
        try:
            logger.info("Prefetch process exiting...")
        except:
            pass
        if not self.context.closed:
            self.context.destroy(0)
        for x in self.procs:
            x.terminate()
        try:
            logger.info("Prefetch process exited.")
        except:
            pass
