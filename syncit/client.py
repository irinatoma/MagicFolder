import os
from os import path
from subprocess import Popen, PIPE

import picklemsg
from probity.walk import walk_path as probity_walk_path

class Client(object):
    def __init__(self, root_path, remote):
        self.root_path = root_path
        self.private_path = path.join(root_path, '.syncit')
        self.remote = remote

    def sync(self):
        if path.isdir(self.root_path):
            assert path.isdir(self.private_path)
            self.merge_versions()
        else:
            os.makedirs(self.private_path)
            self.receive_full_version()

        self.remote.send('quit')
        assert self.remote.recv()[0] == 'bye'

    def receive_full_version(self):
        self.remote.send('stream_latest_version')
        #print "saving latest version to %r" % self.root_path

        msg, payload = self.remote.recv()
        assert msg == 'version_number'
        with open(path.join(self.private_path, 'last_sync'), 'wb') as f:
            f.write("%d\n" % payload)

        while True:
            msg, payload = self.remote.recv()
            if msg == 'done':
                break

            assert msg == 'file_begin'
            #print payload['path']

            file_path = path.join(self.root_path, payload['path'])
            folder_path = path.dirname(file_path)
            if not path.isdir(folder_path):
                os.makedirs(folder_path)

            with open(file_path, 'wb') as local_file:
                self.remote.recv_file(local_file)

    def merge_versions(self):
        last_sync_path = path.join(self.private_path, 'last_sync')
        with open(last_sync_path, 'rb') as f:
            last_version = int(f.read().strip())

        self.remote.send('merge', last_version)
        msg, payload = self.remote.recv()
        assert msg == 'waiting_for_files'

        root_name = path.basename(self.root_path)
        for event in probity_walk_path(self.root_path):
            file_path = event.path.split('/', 1)[1]
            if file_path.startswith('.syncit/'):
                continue

            file_meta = {
                'path': file_path,
                'checksum': event.checksum,
                'size': event.size,
            }
            self.remote.send('file_meta', file_meta)
            msg, payload = self.remote.recv()

            if msg == 'continue':
                continue

            assert msg == 'data'
            #print 'sending data for %r' % file_path
            with open(event.fs_path, 'rb') as data_file:
                self.remote.send_file(data_file)

        self.remote.send('done')

        while True:
            msg, payload = self.remote.recv()
            if msg == 'sync_complete':
                break

            elif msg == 'file_begin':
                file_path = path.join(self.root_path, payload['path'])
                folder_path = path.dirname(file_path)
                if not path.isdir(folder_path):
                    os.makedirs(folder_path)

                with open(file_path, 'wb') as local_file:
                    self.remote.recv_file(local_file)

            elif msg == 'file_remove':
                os.unlink(path.join(self.root_path, payload))

            else:
                assert False, 'unexpected message %r' % msg

        assert payload >= last_version
        #print 'sync complete; now at version %d' % payload
        with open(last_sync_path, 'wb') as f:
            f.write("%d\n" % payload)


def pipe_to_remote(remote_spec):
    hostname, remote_path = remote_spec.split(':')
    script_path = path.join(remote_path, 'sandbox/bin/syncserver')
    child_args = ['ssh', hostname, script_path, remote_path]
    p = Popen(child_args, bufsize=4096, stdin=PIPE, stdout=PIPE, stderr=PIPE)
    return picklemsg.Remote(p.stdout, p.stdin)

def main():
    import sys
    assert len(sys.argv) == 3

    root_path = sys.argv[1]
    remote = pipe_to_remote(sys.argv[2])

    Client(root_path, remote).sync()
