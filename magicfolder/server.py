import sys
import os
from os import path
import traceback
from StringIO import StringIO
import operator
import logging
from contextlib import contextmanager

import picklemsg
from blobdb import BlobDB
from checksum import FileItem, read_version_file, write_version_file

log = logging.getLogger('magicfolder.server')

def dump_fileitems(fh, bag):
    with write_version_file(fh) as write_file_item:
        for i in sorted(bag, key=operator.attrgetter('path')):
            write_file_item(i)

def file_item_tree(file_item_iter):
    return dict( (i.path, i) for i in file_item_iter )

def server_init(root_path):
    os.mkdir(path.join(root_path, 'objects'))
    os.mkdir(path.join(root_path, 'versions'))
    with open(path.join(root_path, 'versions', '0'), 'wb') as f:
        pass

def server_sync(root_path, remote):
    assert path.isdir(root_path)
    data_pool = BlobDB(path.join(root_path, 'objects'))

    def open_version_index(n, mode):
        return open(path.join(root_path, 'versions/%d' % n), mode)

    msg, payload = remote.recv()
    assert msg == 'sync'

    versions_path = path.join(root_path, 'versions')
    latest_version = max(int(v) for v in os.listdir(versions_path))
    remote_base_version = payload

    log.debug("Begin sync at version %d, client last_sync is %d",
              latest_version, remote_base_version)

    with open_version_index(latest_version, 'rb') as f:
        server_bag = set(read_version_file(f))

    if remote_base_version == latest_version:
        remote_outdated = False
        old_bag = server_bag
    else:
        remote_outdated = True
        old_bag = set()
        if remote_base_version != 0:
            with open_version_index(remote_base_version, 'rb') as f:
                old_bag.update(read_version_file(f))

    remote.send('waiting_for_files')

    temp_version_file = StringIO()
    client_bag = set()

    while True:
        msg, payload = remote.recv()
        if msg == 'done':
            break

        assert msg == 'file_meta'
        client_bag.add(payload)

    for i in client_bag:
        if i.checksum not in data_pool:
            log.debug("Downloading data for %s (size: %r, path: %r)",
                      i.checksum, i.size, i.path)
            remote.send('data', i.checksum)
            with data_pool.write_file(i.checksum) as bf:
                remote.recv_file(bf)

    if remote_outdated:
        log.debug("Client was at old version, performing merge")
        for file_item in old_bag - client_bag:
            log.debug("Removed by client: %r", file_item)
        for file_item in client_bag - old_bag:
            log.debug("Added by client: %r", file_item)
        assert old_bag == client_bag
        current_version = latest_version

        for removed_file in client_bag - server_bag:
            assert removed_file.checksum in data_pool
            log.debug("Asking client to remove %s (size: %r, path: %r)",
                      removed_file.checksum, removed_file.size,
                      removed_file.path)
            remote.send('file_remove', removed_file)

        for new_file in server_bag - client_bag:
            log.debug("Sending file %s for path %r",
                      new_file.checksum, new_file.path)
            remote.send('file_begin', new_file)
            with data_pool.read_file(new_file.checksum) as f:
                remote.send_file(f)

    else:
        if server_bag == client_bag:
            current_version = latest_version
            log.debug("Client has no changes, staying at version %d",
                      current_version)
        else:
            current_version = latest_version + 1
            log.debug("Client has changes, creating new version %d",
                      current_version)
            with open_version_index(current_version, 'wb') as f:
                dump_fileitems(f, client_bag)

    log.debug("Sync complete")
    remote.send('sync_complete', current_version)

    msg, payload = remote.recv()
    assert msg == 'quit'
    remote.send('bye')

def calculate_merge(old_bag, client_bag, server_bag):
    """
    "old" is the most recent common ancestor of "client" and "server".
    We need to decide what is safe to keep and remove from each, and
    create a separate list of files with confilcts (they require renaming).
    Here is the logic.

    If a path IS NOT in "old":

                |          server:          |
                | no action   |   created   |
      client: --+-------------+-------------+
      no action | impossible  | keep server |
     -----------+-------------+-------------+
        created | keep client | keep both * |
     -----------+-------------+-------------+

    If a path IS in "old":

                |                 server:                 |
                | no action   |   removed   |   changed   |
      client: --+-------------+-------------+-------------+
      no action |  keep any   |   remove    | keep server |
     -----------+-------------+-------------+-------------+
        removed |   remove    |   remove    | keep server |
     -----------+-------------+-------------+-------------+
        changed | keep client | keep client | keep both * |

    "keep both" means conflict, and one of the files must be renamed.

    """
    client_tree = file_item_tree(client_bag)
    old_tree = file_item_tree(old_bag)
    server_tree = file_item_tree(server_bag)

    client_paths = set(client_tree)
    old_paths = set(old_tree)
    server_paths = set(server_tree)

    # just in case one of the inputs has duplicate paths
    assert len(client_paths) == len(client_bag)
    assert len(old_paths) == len(old_bag)
    assert len(server_paths) == len(server_bag)

    new_tree = {}
    conflict = set()

    for p in client_paths - server_paths - old_paths:
        # new files on the client
        new_tree[p] = client_tree[p]

    for p in server_paths - client_paths - old_paths:
        # new files on the server
        new_tree[p] = server_tree[p]

    for p in client_paths & server_paths - old_paths:
        # new files on both (conflict)
        new_tree[p] = client_tree[p]
        conflict.add(server_tree[p])

    for p in old_paths:
        old_item = old_tree.get(p)
        client_item = client_tree.get(p, None)
        server_item = server_tree.get(p, None)

        if client_item == old_item:
            if server_item == old_item:
                # no change
                new_tree[p] = old_item

            elif server_item is None:
                # removed on server
                pass

            else:
                # changed on server
                new_tree[p] = server_item

        elif client_item is None:
            if server_item == old_item:
                # removed on client
                pass

            elif server_item is None:
                # removed on both
                pass

            else:
                # removed on client but changed on server
                new_tree[p] = server_item

        else:
            if server_item == old_item:
                # changed on client
                new_tree[p] = client_item

            elif server_item is None:
                # don't delete, use client version
                new_tree[p] = client_item

            else:
                # changed on both; conflict
                new_tree[p] = client_item
                conflict.add(server_item)

    return new_tree, conflict


@contextmanager
def try_except_send_remote(remote):
    try:
        yield
    except:
        log.exception("Exception while performing sync")
        try:
            error_report = traceback.format_exc()
        except:
            error_report = "[exception while formatting traceback]"
        remote.send('error', error_report)

def main():
    assert len(sys.argv) == 2
    root_path = path.join(sys.argv[1])

    logging.basicConfig(level=logging.DEBUG,
                        filename=path.join(root_path, 'debug.log'))
    remote = picklemsg.Remote(sys.stdin, sys.stdout)

    with try_except_send_remote(remote):
        server_sync(root_path, remote)
