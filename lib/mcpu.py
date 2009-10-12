#
#

# Copyright (C) 2006, 2007 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.


"""Module implementing the logic behind the cluster operations

This module implements the logic for doing operations in the cluster. There
are two kinds of classes defined:
  - logical units, which know how to deal with their specific opcode only
  - the processor, which dispatches the opcodes to their logical units

"""

import logging
import random
import time

from ganeti import opcodes
from ganeti import constants
from ganeti import errors
from ganeti import rpc
from ganeti import cmdlib
from ganeti import locking


class _LockAcquireTimeout(Exception):
  """Internal exception to report timeouts on acquiring locks.

  """


def _CalculateLockAttemptTimeouts():
  """Calculate timeouts for lock attempts.

  """
  running_sum = 0
  result = [1.0]

  # Wait for a total of at least 150s before doing a blocking acquire
  while sum(result) < 150.0:
    timeout = (result[-1] * 1.05) ** 1.25

    # Cap timeout at 10 seconds. This gives other jobs a chance to run
    # even if we're still trying to get our locks, before finally moving
    # to a blocking acquire.
    if timeout > 10.0:
      timeout = 10.0

    elif timeout < 0.1:
      # Lower boundary for safety
      timeout = 0.1

    result.append(timeout)

  return result


class _LockAttemptTimeoutStrategy(object):
  """Class with lock acquire timeout strategy.

  """
  __slots__ = [
    "_attempt",
    "_random_fn",
    "_start_time",
    "_time_fn",
    ]

  _TIMEOUT_PER_ATTEMPT = _CalculateLockAttemptTimeouts()

  def __init__(self, attempt=0, _time_fn=time.time, _random_fn=random.random):
    """Initializes this class.

    @type attempt: int
    @param attempt: Current attempt number
    @param _time_fn: Time function for unittests
    @param _random_fn: Random number generator for unittests

    """
    object.__init__(self)

    if attempt < 0:
      raise ValueError("Attempt must be zero or positive")

    self._attempt = attempt
    self._time_fn = _time_fn
    self._random_fn = _random_fn

    self._start_time = None

  def NextAttempt(self):
    """Returns the strategy for the next attempt.

    """
    return _LockAttemptTimeoutStrategy(attempt=self._attempt + 1,
                                       _time_fn=self._time_fn,
                                       _random_fn=self._random_fn)

  def CalcRemainingTimeout(self):
    """Returns the remaining timeout.

    """
    try:
      timeout = self._TIMEOUT_PER_ATTEMPT[self._attempt]
    except IndexError:
      # No more timeouts, do blocking acquire
      return None

    # Get start time on first calculation
    if self._start_time is None:
      self._start_time = self._time_fn()

    # Calculate remaining time for this attempt
    remaining_timeout = self._start_time + timeout - self._time_fn()

    # Add a small variation (-/+ 5%) to timeouts. This helps in situations
    # where two or more jobs are fighting for the same lock(s).
    variation_range = remaining_timeout * 0.1
    remaining_timeout += ((self._random_fn() * variation_range) -
                          (variation_range * 0.5))

    assert remaining_timeout >= 0.0, "Timeout must be positive"

    return remaining_timeout


class OpExecCbBase:
  """Base class for OpCode execution callbacks.

  """
  def NotifyStart(self):
    """Called when we are about to execute the LU.

    This function is called when we're about to start the lu's Exec() method,
    that is, after we have acquired all locks.

    """

  def Feedback(self, *args):
    """Sends feedback from the LU code to the end-user.

    """

  def ReportLocks(self, msg):
    """Report lock operations.

    """


class Processor(object):
  """Object which runs OpCodes"""
  DISPATCH_TABLE = {
    # Cluster
    opcodes.OpPostInitCluster: cmdlib.LUPostInitCluster,
    opcodes.OpDestroyCluster: cmdlib.LUDestroyCluster,
    opcodes.OpQueryClusterInfo: cmdlib.LUQueryClusterInfo,
    opcodes.OpVerifyCluster: cmdlib.LUVerifyCluster,
    opcodes.OpQueryConfigValues: cmdlib.LUQueryConfigValues,
    opcodes.OpRenameCluster: cmdlib.LURenameCluster,
    opcodes.OpVerifyDisks: cmdlib.LUVerifyDisks,
    opcodes.OpSetClusterParams: cmdlib.LUSetClusterParams,
    opcodes.OpRedistributeConfig: cmdlib.LURedistributeConfig,
    opcodes.OpRepairDiskSizes: cmdlib.LURepairDiskSizes,
    # node lu
    opcodes.OpAddNode: cmdlib.LUAddNode,
    opcodes.OpQueryNodes: cmdlib.LUQueryNodes,
    opcodes.OpQueryNodeVolumes: cmdlib.LUQueryNodeVolumes,
    opcodes.OpQueryNodeStorage: cmdlib.LUQueryNodeStorage,
    opcodes.OpModifyNodeStorage: cmdlib.LUModifyNodeStorage,
    opcodes.OpRepairNodeStorage: cmdlib.LURepairNodeStorage,
    opcodes.OpRemoveNode: cmdlib.LURemoveNode,
    opcodes.OpSetNodeParams: cmdlib.LUSetNodeParams,
    opcodes.OpPowercycleNode: cmdlib.LUPowercycleNode,
    opcodes.OpEvacuateNode: cmdlib.LUEvacuateNode,
    opcodes.OpMigrateNode: cmdlib.LUMigrateNode,
    # instance lu
    opcodes.OpCreateInstance: cmdlib.LUCreateInstance,
    opcodes.OpReinstallInstance: cmdlib.LUReinstallInstance,
    opcodes.OpRemoveInstance: cmdlib.LURemoveInstance,
    opcodes.OpRenameInstance: cmdlib.LURenameInstance,
    opcodes.OpActivateInstanceDisks: cmdlib.LUActivateInstanceDisks,
    opcodes.OpShutdownInstance: cmdlib.LUShutdownInstance,
    opcodes.OpStartupInstance: cmdlib.LUStartupInstance,
    opcodes.OpRebootInstance: cmdlib.LURebootInstance,
    opcodes.OpDeactivateInstanceDisks: cmdlib.LUDeactivateInstanceDisks,
    opcodes.OpReplaceDisks: cmdlib.LUReplaceDisks,
    opcodes.OpRecreateInstanceDisks: cmdlib.LURecreateInstanceDisks,
    opcodes.OpFailoverInstance: cmdlib.LUFailoverInstance,
    opcodes.OpMigrateInstance: cmdlib.LUMigrateInstance,
    opcodes.OpMoveInstance: cmdlib.LUMoveInstance,
    opcodes.OpConnectConsole: cmdlib.LUConnectConsole,
    opcodes.OpQueryInstances: cmdlib.LUQueryInstances,
    opcodes.OpQueryInstanceData: cmdlib.LUQueryInstanceData,
    opcodes.OpSetInstanceParams: cmdlib.LUSetInstanceParams,
    opcodes.OpGrowDisk: cmdlib.LUGrowDisk,
    # os lu
    opcodes.OpDiagnoseOS: cmdlib.LUDiagnoseOS,
    # exports lu
    opcodes.OpQueryExports: cmdlib.LUQueryExports,
    opcodes.OpExportInstance: cmdlib.LUExportInstance,
    opcodes.OpRemoveExport: cmdlib.LURemoveExport,
    # tags lu
    opcodes.OpGetTags: cmdlib.LUGetTags,
    opcodes.OpSearchTags: cmdlib.LUSearchTags,
    opcodes.OpAddTags: cmdlib.LUAddTags,
    opcodes.OpDelTags: cmdlib.LUDelTags,
    # test lu
    opcodes.OpTestDelay: cmdlib.LUTestDelay,
    opcodes.OpTestAllocator: cmdlib.LUTestAllocator,
    }

  def __init__(self, context):
    """Constructor for Processor

    """
    self.context = context
    self._cbs = None
    self.rpc = rpc.RpcRunner(context.cfg)
    self.hmclass = HooksMaster

  def _ReportLocks(self, level, names, shared, timeout, acquired, result):
    """Reports lock operations.

    @type level: int
    @param level: Lock level
    @type names: list or string
    @param names: Lock names
    @type shared: bool
    @param shared: Whether the locks should be acquired in shared mode
    @type timeout: None or float
    @param timeout: Timeout for acquiring the locks
    @type acquired: bool
    @param acquired: Whether the locks have already been acquired
    @type result: None or set
    @param result: Result from L{locking.GanetiLockManager.acquire}

    """
    parts = []

    # Build message
    if acquired:
      if result is None:
        parts.append("timeout")
      else:
        parts.append("acquired")
    else:
      parts.append("waiting")
      if timeout is None:
        parts.append("blocking")
      else:
        parts.append("timeout=%0.6fs" % timeout)

    parts.append(locking.LEVEL_NAMES[level])

    if names == locking.ALL_SET:
      parts.append("ALL")
    elif isinstance(names, basestring):
      parts.append(names)
    else:
      parts.append(",".join(names))

    if shared:
      parts.append("shared")
    else:
      parts.append("exclusive")

    msg = "/".join(parts)

    logging.debug("LU locks %s", msg)

    if self._cbs:
      self._cbs.ReportLocks(msg)

  def _AcquireLocks(self, level, names, shared, timeout):
    """Acquires locks via the Ganeti lock manager.

    @type level: int
    @param level: Lock level
    @type names: list or string
    @param names: Lock names
    @type shared: bool
    @param shared: Whether the locks should be acquired in shared mode
    @type timeout: None or float
    @param timeout: Timeout for acquiring the locks

    """
    self._ReportLocks(level, names, shared, timeout, False, None)

    acquired = self.context.glm.acquire(level, names, shared=shared,
                                        timeout=timeout)

    self._ReportLocks(level, names, shared, timeout, True, acquired)

    return acquired

  def _ExecLU(self, lu):
    """Logical Unit execution sequence.

    """
    write_count = self.context.cfg.write_count
    lu.CheckPrereq()
    hm = HooksMaster(self.rpc.call_hooks_runner, lu)
    h_results = hm.RunPhase(constants.HOOKS_PHASE_PRE)
    lu.HooksCallBack(constants.HOOKS_PHASE_PRE, h_results,
                     self._Feedback, None)

    if getattr(lu.op, "dry_run", False):
      # in this mode, no post-hooks are run, and the config is not
      # written (as it might have been modified by another LU, and we
      # shouldn't do writeout on behalf of other threads
      self.LogInfo("dry-run mode requested, not actually executing"
                   " the operation")
      return lu.dry_run_result

    try:
      result = lu.Exec(self._Feedback)
      h_results = hm.RunPhase(constants.HOOKS_PHASE_POST)
      result = lu.HooksCallBack(constants.HOOKS_PHASE_POST, h_results,
                                self._Feedback, result)
    finally:
      # FIXME: This needs locks if not lu_class.REQ_BGL
      if write_count != self.context.cfg.write_count:
        hm.RunConfigUpdate()

    return result

  def _LockAndExecLU(self, lu, level, calc_timeout):
    """Execute a Logical Unit, with the needed locks.

    This is a recursive function that starts locking the given level, and
    proceeds up, till there are no more locks to acquire. Then it executes the
    given LU and its opcodes.

    """
    adding_locks = level in lu.add_locks
    acquiring_locks = level in lu.needed_locks
    if level not in locking.LEVELS:
      if self._cbs:
        self._cbs.NotifyStart()

      result = self._ExecLU(lu)

    elif adding_locks and acquiring_locks:
      # We could both acquire and add locks at the same level, but for now we
      # don't need this, so we'll avoid the complicated code needed.
      raise NotImplementedError("Can't declare locks to acquire when adding"
                                " others")

    elif adding_locks or acquiring_locks:
      lu.DeclareLocks(level)
      share = lu.share_locks[level]

      try:
        assert adding_locks ^ acquiring_locks, \
          "Locks must be either added or acquired"

        if acquiring_locks:
          # Acquiring locks
          needed_locks = lu.needed_locks[level]

          acquired = self._AcquireLocks(level, needed_locks, share,
                                        calc_timeout())

          if acquired is None:
            raise _LockAcquireTimeout()

          lu.acquired_locks[level] = acquired

        else:
          # Adding locks
          add_locks = lu.add_locks[level]
          lu.remove_locks[level] = add_locks

          try:
            self.context.glm.add(level, add_locks, acquired=1, shared=share)
          except errors.LockError:
            raise errors.OpPrereqError(
              "Couldn't add locks (%s), probably because of a race condition"
              " with another job, who added them first" % add_locks)

          lu.acquired_locks[level] = add_locks
        try:
          result = self._LockAndExecLU(lu, level + 1, calc_timeout)
        finally:
          if level in lu.remove_locks:
            self.context.glm.remove(level, lu.remove_locks[level])
      finally:
        if self.context.glm.is_owned(level):
          self.context.glm.release(level)

    else:
      result = self._LockAndExecLU(lu, level + 1, calc_timeout)

    return result

  def ExecOpCode(self, op, cbs):
    """Execute an opcode.

    @type op: an OpCode instance
    @param op: the opcode to be executed
    @type cbs: L{OpExecCbBase}
    @param cbs: Runtime callbacks

    """
    if not isinstance(op, opcodes.OpCode):
      raise errors.ProgrammerError("Non-opcode instance passed"
                                   " to ExecOpcode")

    self._cbs = cbs
    try:
      lu_class = self.DISPATCH_TABLE.get(op.__class__, None)
      if lu_class is None:
        raise errors.OpCodeUnknown("Unknown opcode")

      timeout_strategy = _LockAttemptTimeoutStrategy()

      while True:
        try:
          acquire_timeout = timeout_strategy.CalcRemainingTimeout()

          # Acquire the Big Ganeti Lock exclusively if this LU requires it,
          # and in a shared fashion otherwise (to prevent concurrent run with
          # an exclusive LU.
          if self._AcquireLocks(locking.LEVEL_CLUSTER, locking.BGL,
                                not lu_class.REQ_BGL, acquire_timeout) is None:
            raise _LockAcquireTimeout()

          try:
            lu = lu_class(self, op, self.context, self.rpc)
            lu.ExpandNames()
            assert lu.needed_locks is not None, "needed_locks not set by LU"

            return self._LockAndExecLU(lu, locking.LEVEL_INSTANCE,
                                       timeout_strategy.CalcRemainingTimeout)
          finally:
            self.context.glm.release(locking.LEVEL_CLUSTER)

        except _LockAcquireTimeout:
          # Timeout while waiting for lock, try again
          pass

        timeout_strategy = timeout_strategy.NextAttempt()

    finally:
      self._cbs = None

  def _Feedback(self, *args):
    """Forward call to feedback callback function.

    """
    if self._cbs:
      self._cbs.Feedback(*args)

  def LogStep(self, current, total, message):
    """Log a change in LU execution progress.

    """
    logging.debug("Step %d/%d %s", current, total, message)
    self._Feedback("STEP %d/%d %s" % (current, total, message))

  def LogWarning(self, message, *args, **kwargs):
    """Log a warning to the logs and the user.

    The optional keyword argument is 'hint' and can be used to show a
    hint to the user (presumably related to the warning). If the
    message is empty, it will not be printed at all, allowing one to
    show only a hint.

    """
    assert not kwargs or (len(kwargs) == 1 and "hint" in kwargs), \
           "Invalid keyword arguments for LogWarning (%s)" % str(kwargs)
    if args:
      message = message % tuple(args)
    if message:
      logging.warning(message)
      self._Feedback(" - WARNING: %s" % message)
    if "hint" in kwargs:
      self._Feedback("      Hint: %s" % kwargs["hint"])

  def LogInfo(self, message, *args):
    """Log an informational message to the logs and the user.

    """
    if args:
      message = message % tuple(args)
    logging.info(message)
    self._Feedback(" - INFO: %s" % message)


class HooksMaster(object):
  """Hooks master.

  This class distributes the run commands to the nodes based on the
  specific LU class.

  In order to remove the direct dependency on the rpc module, the
  constructor needs a function which actually does the remote
  call. This will usually be rpc.call_hooks_runner, but any function
  which behaves the same works.

  """
  def __init__(self, callfn, lu):
    self.callfn = callfn
    self.lu = lu
    self.op = lu.op
    self.env, node_list_pre, node_list_post = self._BuildEnv()
    self.node_list = {constants.HOOKS_PHASE_PRE: node_list_pre,
                      constants.HOOKS_PHASE_POST: node_list_post}

  def _BuildEnv(self):
    """Compute the environment and the target nodes.

    Based on the opcode and the current node list, this builds the
    environment for the hooks and the target node list for the run.

    """
    env = {
      "PATH": "/sbin:/bin:/usr/sbin:/usr/bin",
      "GANETI_HOOKS_VERSION": constants.HOOKS_VERSION,
      "GANETI_OP_CODE": self.op.OP_ID,
      "GANETI_OBJECT_TYPE": self.lu.HTYPE,
      "GANETI_DATA_DIR": constants.DATA_DIR,
      }

    if self.lu.HPATH is not None:
      lu_env, lu_nodes_pre, lu_nodes_post = self.lu.BuildHooksEnv()
      if lu_env:
        for key in lu_env:
          env["GANETI_" + key] = lu_env[key]
    else:
      lu_nodes_pre = lu_nodes_post = []

    return env, frozenset(lu_nodes_pre), frozenset(lu_nodes_post)

  def _RunWrapper(self, node_list, hpath, phase):
    """Simple wrapper over self.callfn.

    This method fixes the environment before doing the rpc call.

    """
    env = self.env.copy()
    env["GANETI_HOOKS_PHASE"] = phase
    env["GANETI_HOOKS_PATH"] = hpath
    if self.lu.cfg is not None:
      env["GANETI_CLUSTER"] = self.lu.cfg.GetClusterName()
      env["GANETI_MASTER"] = self.lu.cfg.GetMasterNode()

    env = dict([(str(key), str(val)) for key, val in env.iteritems()])

    return self.callfn(node_list, hpath, phase, env)

  def RunPhase(self, phase, nodes=None):
    """Run all the scripts for a phase.

    This is the main function of the HookMaster.

    @param phase: one of L{constants.HOOKS_PHASE_POST} or
        L{constants.HOOKS_PHASE_PRE}; it denotes the hooks phase
    @param nodes: overrides the predefined list of nodes for the given phase
    @return: the processed results of the hooks multi-node rpc call
    @raise errors.HooksFailure: on communication failure to the nodes
    @raise errors.HooksAbort: on failure of one of the hooks

    """
    if not self.node_list[phase] and not nodes:
      # empty node list, we should not attempt to run this as either
      # we're in the cluster init phase and the rpc client part can't
      # even attempt to run, or this LU doesn't do hooks at all
      return
    hpath = self.lu.HPATH
    if nodes is not None:
      results = self._RunWrapper(nodes, hpath, phase)
    else:
      results = self._RunWrapper(self.node_list[phase], hpath, phase)
    errs = []
    if not results:
      msg = "Communication Failure"
      if phase == constants.HOOKS_PHASE_PRE:
        raise errors.HooksFailure(msg)
      else:
        self.lu.LogWarning(msg)
        return results
    for node_name in results:
      res = results[node_name]
      if res.offline:
        continue
      msg = res.fail_msg
      if msg:
        self.lu.LogWarning("Communication failure to node %s: %s",
                           node_name, msg)
        continue
      for script, hkr, output in res.payload:
        if hkr == constants.HKR_FAIL:
          if phase == constants.HOOKS_PHASE_PRE:
            errs.append((node_name, script, output))
          else:
            if not output:
              output = "(no output)"
            self.lu.LogWarning("On %s script %s failed, output: %s" %
                               (node_name, script, output))
    if errs and phase == constants.HOOKS_PHASE_PRE:
      raise errors.HooksAbort(errs)
    return results

  def RunConfigUpdate(self):
    """Run the special configuration update hook

    This is a special hook that runs only on the master after each
    top-level LI if the configuration has been updated.

    """
    phase = constants.HOOKS_PHASE_POST
    hpath = constants.HOOKS_NAME_CFGUPDATE
    nodes = [self.lu.cfg.GetMasterNode()]
    self._RunWrapper(nodes, hpath, phase)
