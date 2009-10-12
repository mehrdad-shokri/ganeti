#!/usr/bin/python
#

# Copyright (C) 2009 Google Inc.
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


"""Script for unittesting the mcpu module"""


import unittest

from ganeti import mcpu


class TestLockAttemptTimeoutStrategy(unittest.TestCase):
  def testConstants(self):
    tpa = mcpu._LockAttemptTimeoutStrategy._TIMEOUT_PER_ATTEMPT
    self.assert_(len(tpa) > 10)
    self.assert_(sum(tpa) >= 150.0)

  def testSimple(self):
    strat = mcpu._LockAttemptTimeoutStrategy(_random_fn=lambda: 0.5,
                                             _time_fn=lambda: 0.0)

    self.assertEqual(strat._attempt, 0)

    prev = None
    for _ in range(len(mcpu._LockAttemptTimeoutStrategy._TIMEOUT_PER_ATTEMPT)):
      timeout = strat.CalcRemainingTimeout()
      self.assert_(timeout is not None)

      self.assert_(timeout <= 10.0)
      self.assert_(prev is None or timeout >= prev)

      strat = strat.NextAttempt()

      prev = timeout

    self.assert_(strat.CalcRemainingTimeout() is None)


if __name__ == "__main__":
  unittest.main()
