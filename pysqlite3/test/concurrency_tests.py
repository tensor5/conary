#!/usr/bin/env python
import testsupport
import os, sys, tempfile, time, unittest
import sqlite3 as sqlite

class Waiter:
    def wait(self, data, count):
        self.count = count
        if count > 5:
            return 0
        return 1

    def __init__(self):
        self.count = 0

class ConcurrencyTests(unittest.TestCase):
    def CheckSingleProcessTimeouts(self):
        fd, dbfile = tempfile.mkstemp()
        os.close(fd)

        # 2 second timeout
        db1 = sqlite.connect(dbfile, timeout=2000)
        db2 = sqlite.connect(dbfile)
        cu2 = db2.cursor()

        cu1 = db1.cursor()
        cu1.execute('CREATE TABLE foo (bar)')
        db1.commit()

        cu2.execute('INSERT INTO foo VALUES(1)')
        t1 = time.time()
        try:
            cu1.execute('INSERT INTO foo VALUES(2)')
        except sqlite.InternalError, e:
            assert(str(e) == "database is locked")
        t2 = time.time()
        # make sure we slept 2 seconds
        assert(t2 - t1 >= 2)
        db2.commit()
        cu1.stmt.step()
        assert [ x for x in cu1.execute('select * from foo') ] == [(1,), (2,)] 

    def CheckSingleProcessCallbacks(self):
        fd, dbfile = tempfile.mkstemp()
        os.close(fd)

        # 2 second timeout
        db1 = sqlite.connect(dbfile)
        waiter = Waiter()
        db1.db.sqlite_busy_handler(waiter.wait)
        db2 = sqlite.connect(dbfile)
        cu2 = db2.cursor()

        cu1 = db1.cursor()
        cu1.execute('CREATE TABLE foo (bar)')
        db1.commit()

        cu2.execute('INSERT INTO foo VALUES(1)')
        t1 = time.time()
        try:
            cu1.execute('INSERT INTO foo VALUES(2)')
        except sqlite.InternalError, e:
            assert(str(e) == "database is locked")
        t2 = time.time()
        print waiter.count
        assert(waiter.count == 6)
        db2.commit()
        cu1.stmt.step()
        assert [ x for x in cu1.execute('select * from foo') ] == [(1,), (2,)] 


def suite():
    concurrency_suite = unittest.makeSuite(ConcurrencyTests, "Check")
    test_suite = unittest.TestSuite((concurrency_suite,))
    return test_suite

def main():
    runner = unittest.TextTestRunner()
    runner.run(suite())

if __name__ == "__main__":
    main()
        
        
