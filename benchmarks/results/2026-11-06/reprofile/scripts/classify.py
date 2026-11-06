import pickle, re, sys, collections, functools
S=sys.argv[1]; N=5000
GENERIC_RE = re.compile(r"^(__mem|memcpy|memmove|memset|__memset|__memcmp|memcmp|__strlen|strlen|mi_|_mi_|malloc|free|realloc|calloc|cfree|__libc_|_int_|__tls_get_addr|_PyObject_Malloc|_PyObject_Free|PyObject_Malloc|PyObject_Free|PyMem_|_PyMem|_Py_Dealloc|_Py_dealloc|Py_DecRef|Py_IncRef|_Py_DecRef|_Py_IncRef|core::ptr::drop_in_place|<alloc::|alloc::|__rust_|__rdl_|<core::|core::|<std::sync|std::sync|<std::thread|std::thread|std::sys::sync|<std::sys::sync|_dl_|__lll|pthread_|___pthread|__pthread|syscall|futex)")
CPY_GENERIC_RE = re.compile(r"^(Py|_Py|dict|tuple|list|long|float|unicode|object_|type_|subtype|frame_|cell_|method_|meth_|func_|insertdict|setitem|copy_lock|lookdict|unicodekeys|new_keys|new_dict|free_keys|clone_combined|vectorcall|slot_|set_|insert_|find_|bytes|_copy|compare|insertion|build_|anextawaitable|delitem|cfunction|clear_|visit_|getset|mappingproxy|wrapperdescr|wrap_|unicode_|module_|tp_|PyCMethod|_PyStack|subtype_dealloc|_Py_|_PyArg)")
def bucket(fn, ob):
    o=(ob or "").split("/")[-1]
    if "_native" in o:
        if re.search(r"bessemer_codec", fn): return "rust_serialise"
        if re.search(r"bessemer_loop", fn): return "loop"
        if re.search(r"drop_deferred_references", fn): return "bessemer_native_orch"
        if re.search(r"pyo3::|PyGIL|gil", fn) and not re.search(r"_native::", fn): return "pyo3_gil"
        if re.search(r"_native::(invoker|bridge|conversion|model)", fn): return "bessemer_native_orch"
        if GENERIC_RE.match(fn): return None
        if re.search(r"tokio::runtime::scheduler::multi_thread|tokio::runtime::blocking|tokio::runtime::park|Launch>::launch", fn): return "rust_runtime"
        return "rust_http"
    if "_asyncio" in o or re.search(r"^(_asyncio_|task_|TaskObj|FutureObj|future_|Future|swap_current_task|enter_task|leave_task|register_eager|unregister_eager|TaskStepMethWrapper|FutureIter|PyRunningLoopHolder|get_running_loop|_asyncio_get_running_loop)", fn): return "orch_task"
    if "select" in o or re.search(r"^(select_epoll|pyepoll|epoll_|newPyEpoll|sock_recv|sock_send|sock_|internal_select|socket_)", fn): return "loop"
    if "libc" in o or "ld-linux" in o:
        if re.search(r"epoll_wait|sendto|recvfrom|send$|recv$|__libc_send|__libc_recv|read$|write$", fn): return "syscall_wrap"
        if GENERIC_RE.match(fn): return None
        if re.search(r"clock_gettime|__vdso", fn): return "rust_http"
        return None
    if "python" in o or o in ("???",""):
        if re.search(r"^(take_gil|drop_gil|PyEval_RestoreThread|PyEval_SaveThread|PyGILState|_PyThreadState|PyThreadState|_PyEval_ReleaseLock|_PyEval_AcquireLock|tstate_)", fn): return "pyo3_gil"
        if re.search(r"^(context_|PyContext|contextvar_|hamt_|_PyContext|PyContextVar|_PyHamt|token_)", fn): return "orch_context"
        if re.search(r"_PyEval_EvalFrameDefault|^gen_|_PyEval_Vector|_PyFunction_Vectorcall|^_PyEval_|PyIter_Send|_PyGen|^coro_|gen_send", fn): return "py_eval"
        return None
    if "vdso" in o: return "rust_http"
    return None
def run(name):
    selfc,obj,calls,summary=pickle.load(open(f"{S}/{name}.pkl","rb"))
    callers=collections.defaultdict(list)
    for (a,b),c in calls.items(): callers[b].append((a,c))
    memo={}
    def dist(f, depth=0, stack=()):
        if f in memo: return memo[f]
        b=bucket(f,obj.get(f))
        if b: memo[f]={b:1.0}; return memo[f]
        if depth>40 or f in stack: return {"unattributed":1.0}
        cs=callers.get(f,[]); tot=sum(c for _,c in cs)
        if not tot: return {"unattributed":1.0}
        d=collections.Counter()
        for a,c in cs:
            for k,v in dist(a,depth+1,stack+(f,)).items(): d[k]+=v*c/tot
        if depth==0: memo[f]=dict(d)
        return dict(d)
    sys.setrecursionlimit(10000)
    out=collections.Counter()
    for f,c in selfc.items():
        for k,v in dist(f).items(): out[k]+=c*v
    tot=sum(selfc.values())
    return {k:v/N for k,v in out.items()}, tot/N, summary/N
import json
res={}
for n in ["bessemer-json","bessemer-items","bessemer-echo","asyncio-json","asyncio-items","asyncio-echo"]:
    o,t,s=run(n); res[n]=o
    print(n, f"total {t:,.0f} summary {s:,.0f}")
    for k,v in sorted(o.items(), key=lambda x:-x[1]): print(f"   {k:22s} {v:8,.0f}  {100*v/t:5.1f}%")
json.dump(res,open(f"{S}/buckets.json","w"),indent=1)
