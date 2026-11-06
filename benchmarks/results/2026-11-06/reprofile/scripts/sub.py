import pickle,collections,re,sys
sys.path.insert(0,".")
exec(open("classify.py").read().split("import json")[0].replace("S=sys.argv[1]; N=5000","S='.'; N=5000"))
def fam(f):
    for pat,name in [(r"httparse|_native::http1::(build|scan|parse)|HeaderMap|header::map|header::name|HeaderValue|http::","http1 parse + header map"),
                     (r"_native::http1::(write_parts|write_date|Writer)|write_all|DirectHeaders","http1 response write"),
                     (r"_native::http1","http1 serve loop / body"),
                     (r"tokio::runtime::io|tokio::io|mio::|Registration|ScheduledIo|Readiness","tokio I/O driver"),
                     (r"tokio::time|tokio::runtime::time|Sleep|timer|Timespec|clock_gettime|Instant|SystemTime|vdso","timers + clock reads"),
                     (r"tokio::","tokio scheduler / sync"),
                     (r"_native::router|Dispatcher","router/dispatch"),
                     (r"_native::controls","controls (request id, limits)"),
                     (r"_native::transport","transport checks"),
                     (r"_native::telemetry","telemetry"),
                     (r"_native::budget","budget"),
                     (r"_native::handle|_native::lib|_native::serve|_native::","handle (lib.rs)")]:
        if re.search(pat,f): return name
    return "other rust"
for n in sys.argv[1:]:
    selfc,obj,calls,summary=pickle.load(open(f"{n}.pkl","rb"))
    callers=collections.defaultdict(list)
    for (a,b),c in calls.items(): callers[b].append((a,c))
    memo={}
    def dist(f,depth=0,stack=()):
        if f in memo: return memo[f]
        b=bucket(f,obj.get(f))
        if b: 
            r={(b,fam(f) if b=="rust_http" else ""):1.0}; memo[f]=r; return r
        if depth>40 or f in stack: return {("unattr",""):1.0}
        cs=callers.get(f,[]); tot=sum(c for _,c in cs)
        if not tot: return {("unattr",""):1.0}
        d=collections.Counter()
        for a,c in cs:
            for k,v in dist(a,depth+1,stack+(f,)).items(): d[k]+=v*c/tot
        if depth==0: memo[f]=dict(d)
        return dict(d)
    out=collections.Counter()
    for f,c in selfc.items():
        for k,v in dist(f).items():
            if k[0]=="rust_http": out[k[1]]+=c*v/5000
    print(n)
    for k,v in out.most_common(): print(f"   {k:32s}{v:8,.0f}")
