import json
import uuid

import tornado.ioloop
import tornado.stack_context

from imc import async
from imc import auth

class Connection:
    def __init__(self,linkclass,linkid):
        self.linkclass = linkclass
        self.linkid = linkid
        self.link_linkidmap = {}
        self._close_callback = []

    def send_msg(self,data):
        pass

    def start_recv(self,recv_callback):
        pass

    def add_close_callback(self,callback):
        self._close_callback.append(tornado.stack_context.wrap(callback))

    def close(self):
        for callback in self._close_callback:
            callback(self)

class Proxy:
    def __init__(self,linkclass,linkid,auth_instance,conn_linkid_fn = None):
        self.MSGTYPE_CALL = 'call'
        self.MSGTYPE_RET = 'ret'
        self.MSGTYPE_SENDFILE = 'sendfile'
        self.MSGTYPE_RECVFILE = 'recvfile'

        self._ioloop = tornado.ioloop.IOLoop.instance()
        self._linkclass = linkclass
        self._linkid = linkid
        self._auth = auth_instance

        if conn_linkid_fn == None:
            self._conn_linkid_fn = lambda : None
        else:
            self._conn_linkid_fn = conn_linkid_fn

        self._conn_linkidmap = {}
        self._conn_retidmap = {self._linkid:{}}
        self._call_pathmap = {}

        self._conn_send_filekeymap = {self._linkid:{}}
        self._conn_recv_filekeymap = {self._linkid:{}}

        self._check_waitcaller_timer = tornado.ioloop.PeriodicCallback(self._check_waitcaller,1000)
        self._check_waitcaller_timer.start()

        Proxy.instance = self 

    def add_conn(self,conn):
        assert conn.linkid not in self._conn_linkidmap 

        self._conn_linkidmap[conn.linkid] = conn
        self._conn_retidmap[conn.linkid] = {}

        self._conn_send_filekeymap[conn.linkid] = {}
        self._conn_recv_filekeymap[conn.linkid] = {}

        conn.add_close_callback(self._conn_close_cb)
        conn.start_recv(self._recv_dispatch)

    def link_conn(self,linkid,conn):
        assert conn.linkid in self._conn_linkidmap 

        conn.link_linkidmap[linkid] = True
        self._conn_linkidmap[linkid] = conn
    
    def unlink_conn(self,linkid):
        assert linkid in self._conn_linkidmap 

        conn = self._conn_linkidmap.pop(linkid)
        del conn.link_linkidmap[linkid]

    def del_conn(self,conn):
        wait_map = self._conn_retidmap[conn.linkid]
        waits = wait_map.items()
        wait_ret = []
        for retid,wait in waits:
            wait_ret.append((wait['caller_linkid'],retid))

        for linkid,retid in wait_ret:
            self._ret_call(linkid,retid,(False,'Eclose'))

        linkids = conn.link_linkidmap.keys()
        link_del = []
        for linkid in linkids:
            link_del.append(linkid)

        for linkid in link_del:
            self.unlink_conn(linkid)

        del self._conn_linkidmap[conn.linkid]
        del self._conn_retidmap[conn.linkid]

        del self._conn_send_filekeymap[conn.linkid]
        del self._conn_recv_filekeymap[conn.linkid]

    def get_conn(self,linkid):
        try:
            return self._conn_linkidmap[linkid]

        except KeyError:
            return None

    def register_call(self,path,func_name,func):
        self._call_pathmap[''.join([path,func_name])] = func

    def call(self,caller_grid,timeout,idendesc,dst,func_name,param):
        caller_retid = ''.join([self._linkid,'/',caller_grid])
        return self._route_call(self._linkclass,self._linkid,caller_retid,timeout,idendesc,dst,func_name,param)

    def sendfile(self,dst_linkid,load_path):
        conn = self._request_conn(dst_linkid)

        fd = os.open(load_path,os.O_RDONLY)
        filekey = uuid.uuid4()
        filesize = os.fstat(fd).st_size

        self._conn_send_filekeymap[conn.linkid][filekey] = {
            'fd':fd,
            'filesize':filesize       
        }

        self._send_msg_sendfile(conn,filekey,filesize)

        return filekey

    def recvfile(self,filekey,save_path):

        return

    def _route_call(self,conn_linkclass,conn_linkid,caller_retid,timeout,idendesc,dst,func_name,param):
        def __add_wait_caller(conn_linkid):
            self._conn_retidmap[conn_linkid][caller_retid] = {
                'caller_linkid':caller_linkid,
                'timeout':timeout
            }

        def __ret(result):
            if caller_linkid == self._linkid:
                return result

            else:
                conn = self._request_conn(caller_linkid)
                if conn != None:
                    self._send_msg_ret(conn,caller_linkid,caller_retid,result)

        iden = self._auth.get_iden(conn_linkclass,conn_linkid,idendesc)
        if iden == None:
            return __ret(False,'Eilliden')

        dst_part = dst.split('/',3)
        dst_linkid = dst_part[2]
        dst_path = dst_part[3]

        caller_linkid = iden['linkid']
        assert caller_retid.split('/',1)[0] == caller_linkid

        if dst_linkid == self._linkid:
            __add_wait_caller(self._linkid)

            try:
                old_iden = self._auth.change_iden(iden)
                result = self._call_pathmap[''.join([dst_path,func_name])](iden,param)
                self._auth.change_iden(old_iden)

            except KeyError:
                result = (False,'Enoexist')

            del self._conn_retidmap[self._linkid][caller_retid]

            return __ret(result)

        else:
            conn = self._request_conn(dst_linkid)
            if conn == None:
                return __ret((False,'Enoexist'))

            else:
                if caller_linkid == self._linkid:
                    __add_wait_caller(conn.linkid)
                    self._send_msg_call(conn,caller_retid,timeout,idendesc,dst,func_name,param)

                    result = async.switchtop()
                    del self._conn_retidmap[conn.linkid][caller_retid]

                    return __ret(result)

                else:
                    self._send_msg_call(conn,caller_retid,timeout,idendesc,dst,func_name,param)

                    return

    def _ret_call(self,caller_linkid,caller_retid,result):
        @async.caller
        def __ret_remote():
            conn = self._request_conn(caller_linkid)
            if conn != None:
                self._send_msg_ret(conn,caller_linkid,caller_retid,result)

        if caller_linkid == self._linkid:
            grid = caller_retid.split('/',1)[1]
            async.retcall(grid,result)

        else:
            __ret_remote()

    def _request_conn(self,linkid):
        try:
            return self._conn_linkidmap[linkid]

        except KeyError:
            conn = self._conn_linkid_fn(linkid)

            if conn != None and conn.linkid != linkid:
                self.link_conn(linkid,conn)

            return conn

    def _recv_dispatch(self,conn,data):
        msg = json.loads(data.decode('utf-8'))
        msg_type = msg['type']

        if msg_type == self.MSGTYPE_CALL:
            self._recv_msg_call(conn,msg)

        elif msg_type == self.MSGTYPE_RET:
            self._recv_msg_ret(conn,msg)

        elif msg_type == self.MSGTYPE_SENDFILE:
            self._recv_msg_sendfile(conn,msg)

        elif msg_type == self.MSGTYPE_RECVFILE:
            self._recv_msg_recvfile(conn,msg)

    def _conn_close_cb(self,conn):
        self.del_conn(conn)
        print('connection close')
    
    def _check_waitcaller(self):
        wait_maps = self._conn_retidmap.values()
        for wait_map in wait_maps:
            waits = wait_map.items()
            wait_ret = []
            for retid,wait in waits:
                wait['timeout'] -= 1000

                if wait['timeout'] <= 0:
                    wait_ret.append((wait['caller_linkid'],retid))

            for linkid,retid in wait_ret:
                self._ret_call(linkid,retid,(False,'Etimeout'))

    def _send_msg_call(self,conn,caller_retid,timeout,idendesc,dst,func_name,param):
        msg = {
            'type':self.MSGTYPE_CALL,
            'caller_retid':caller_retid,
            'timeout':timeout,
            'idendesc':idendesc,
            'dst':dst,
            'func_name':func_name,
            'param':param
        }

        conn.send_msg(bytes(json.dumps(msg),'utf-8')) 

    def _recv_msg_call(self,conn,msg):
        @async.caller
        def __call():
            self._route_call(conn.linkclass,conn.linkid,caller_retid,timeout,idendesc,dst,func_name,param)

        caller_retid = msg['caller_retid']
        timeout = msg['timeout']
        idendesc = msg['idendesc']
        dst = msg['dst']
        func_name = msg['func_name']
        param = msg['param']

        __call()

    def _send_msg_ret(self,conn,caller_linkid,caller_retid,result):
        stat,data = result
        msg = {
            'type':self.MSGTYPE_RET,
            'caller_linkid':caller_linkid,
            'caller_retid':caller_retid,
            'result':{'stat':stat,'data':data}
        }

        conn.send_msg(bytes(json.dumps(msg),'utf-8'))

    def _recv_msg_ret(self,conn,msg):
        caller_linkid = msg['caller_linkid']
        caller_retid = msg['caller_retid']
        data = msg['result']
        result = (data['stat'],data['data'])

        self._ret_call(caller_linkid,caller_retid,result)

    def _send_msg_sendfile(self,conn,filekey,filesize):
        msg = {
            'type':self.MSGTYPE_SENDFILE,
            'filekey':filekey,
            'filesize':filesize
        }

        conn.send_msg(bytes(json.dumps(msg),'utf-8'))

    def _recv_msg_sendfile(self,conn,msg):
        filekey = msg['filekey']
        filesize = msg['filesize']


@async.callee
def imc_call(idendesc,dst,func_name,param,_grid):
    return Proxy.instance.call(_grid,1000,idendesc,dst,func_name,param)

def imc_call_async(idendesc,dst,func_name,param,callback = None):
    @async.caller
    def func():
        ret = imc_call(idendesc,dst,func_name,param)
        if callback != None:
            callback(ret)

    func()

def imc_register_call(path,func_name,func):
    Proxy.instance.register_call(path,func_name,func)
