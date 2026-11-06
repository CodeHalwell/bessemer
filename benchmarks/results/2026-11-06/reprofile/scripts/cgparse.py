import re, sys, collections, pickle
def parse(path):
    names = {"fn": {}, "ob": {}}
    def unc(kind, s):
        m = re.match(r"\((\d+)\)(?: (.*))?$", s)
        if not m: return s
        if m.group(2) is not None: names[kind][m.group(1)] = m.group(2); return m.group(2)
        return names[kind][m.group(1)]
    selfc = collections.Counter(); obj = {}; calls = collections.Counter()  # (caller, callee) -> inclusive
    fn = ob = None; cfn = None; cob=None; after = False; summary=0
    for line in open(path):
        line=line.rstrip("\n")
        if line.startswith("summary:"): summary=int(line.split()[1]); continue
        if line.startswith("ob="): ob=unc("ob",line[3:]); continue
        if line.startswith("fn="): fn=unc("fn",line[3:]); fn=fn.replace("'2",""); obj.setdefault(fn,ob); after=False; continue
        if line.startswith("cob="): cob=unc("ob",line[4:]); continue
        if line.startswith("cfn="): cfn=unc("fn",line[4:]).replace("'2",""); continue
        if line.startswith(("fl=","fi=","fe=","cfi=","cfl=")): continue
        if line.startswith("calls="): after=True; continue
        if line and (line[0].isdigit() or line[0] in "+-*"):
            p=line.split()
            if len(p)<2: continue
            c=int(p[1])
            if after:
                after=False
                if cfn!=fn: calls[(fn,cfn)]+=c
            else: selfc[fn]+=c
    return selfc, obj, calls, summary
if __name__=="__main__":
    r=parse(sys.argv[1]); pickle.dump(r,open(sys.argv[2],"wb"))
