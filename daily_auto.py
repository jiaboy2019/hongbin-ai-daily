import base64, io, json, math, os, re, urllib.request, urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from PIL import Image,ImageDraw,ImageFont,ImageOps
from playwright.sync_api import sync_playwright
D={"issue":"012","date":"2026-11-25","headline":{"title":"小豆开启邀请制公测","body":"豆包旗下独立应用“小豆”开放体验。凭邀请码可领免费额度，授权后可记待办、查订单、跟进退换货。","source":"豆包官方、IT之家"},"updates":[{"tag":"医疗健康","title":"蚂蚁开源健康风险预测模型","body":"LingSante Pulse 综合心电、脑电等生理信号，研究未来六年内150种疾病表型的风险预测。模型结果仍需临床验证。","source":"量子位"},{"tag":"企业应用","title":"Snowflake 追踪员工为何用AI超预算","body":"公司会回看超支员工用AI完成了哪些任务，寻找值得推广的工作方法，再评估其他团队能否复用。","source":"VentureBeat"},{"tag":"数字音乐","title":"Tidal 将清理完全由AI生成的歌曲","body":"12月9日起，平台将陆续移除完全由生成式AI制作的曲目。负责人预计约2900万首歌曲受到影响。","source":"Engadget"}],"leaders":[{"name":"丘成桐｜清华大学求真书院院长","title":"AI给出证明，人还要追问为什么","body":"谈及AI辅助数学证明，丘成桐指出，提出重要问题本身就需要很高的创造力。","source":"科技日报"},{"name":"查尔斯·拉曼纳｜微软执行副总裁","title":"手写代码少了，设计和验证更重要","body":"拉曼纳认为，系统如何设计、结果怎样测试、何时可以上线，仍需要工程师作出判断。","source":"IT之家"},{"name":"瓦斯·纳拉辛汉｜诺华首席执行官","title":"对AI五到十年内治愈癌症持保留态度","body":"纳拉辛汉更看好AI缩短药物研发周期。他认为，人类对疾病机制的了解仍然有限。","source":"IT之家"}]}
out=Path("output");out.mkdir(exist_ok=True)
def fetch(url):
    req=urllib.request.Request(url,headers={"User-Agent":"HongbinDaily/1.0 (editorial illustration; github.com/jiaboy2019/hongbin-ai-daily)"})
    with urllib.request.urlopen(req,timeout=18) as f: return f.read()
def enc(im):
    buf=io.BytesIO();im.convert("RGB").save(buf,"JPEG",quality=90)
    return "data:image/jpeg;base64,"+base64.b64encode(buf.getvalue()).decode()
font="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
def graphic(i):
    bg,fg=[("#e2ece9","#236853"),("#e4edf0","#28647c"),("#e0edf6","#2c5777"),("#eee5ed","#65436a")][i]
    im=Image.new("RGB",(540,354),bg);d=ImageDraw.Draw(im)
    if i==0:
        d.rounded_rectangle((56,53,484,304),radius=28,fill="white");d.text((93,72),"小豆",font=ImageFont.truetype(font,70),fill=fg);d.line((97,189,438,189),fill=bg,width=4);d.text((97,207),"邀请制公测",font=ImageFont.truetype(font,42),fill=fg)
    elif i==1:
        for x in range(0,541,27): d.line((x,0,x,354),fill="#d2e0e3")
        for y in range(0,354,27): d.line((0,y,540,y),fill="#d2e0e3")
        d.line([(0,195),(88,195),(115,168),(143,201),(176,197),(200,222),(239,56),(257,277),(286,169),(310,195),(387,195),(414,173),(446,195),(540,195)],fill=fg,width=9)
    elif i==2:
        for j,h in enumerate([70,111,93,161,214]):d.rounded_rectangle((60+j*83,288-h,111+j*83,288),radius=8,fill=fg)
        for x in range(24,515,24): d.line((x,137,x+12,137),fill="#c56447",width=5)
    else:
        for j in range(27):
            h=int(22+85*abs(math.sin(j*.51))*abs(math.cos(j*.16)));x=25+19*j;d.rounded_rectangle((x,177-h,x+10,177+h),radius=5,fill=fg)
    return enc(im)
def photo(n):
    if n==1: urls=["https://www.microsoft.com/en-us/power-platform/blog/wp-content/uploads/2020/02/MS-Exec-BAP-Lamanna-Charles-172-scaled.jpg"]
    else:
        page=["Shing-Tung_Yau","","Vasant_Narasimhan"][n];urls=[]
        for api in ["https://en.wikipedia.org/api/rest_v1/page/summary/"+page,"https://en.wikipedia.org/w/api.php?action=query&prop=pageimages&pithumbsize=500&format=json&titles="+page]:
            try:
                res=json.loads(fetch(api));item=(next(iter(res["query"]["pages"].values())) if "query" in res else res)
                urls.extend(item[k]["source"] for k in ["thumbnail","originalimage"] if k in item)
                if urls:break
            except Exception:pass
        if not urls:
            html=fetch("https://en.wikipedia.org/wiki/"+page).decode()
            from html import unescape
            m=re.search(r'<meta property="og:image" content="([^"]+)',html)
            if m:urls.append(unescape(m.group(1)))
    for u in urls:
        try:
            im=ImageOps.exif_transpose(Image.open(io.BytesIO(fetch(u)))).convert("RGB")
            return enc(ImageOps.fit(im,(480,318),centering=(.5,.26)))
        except Exception:pass
    raise RuntimeError("Missing verified portrait "+str(n))
assets={k:graphic(i) for i,k in enumerate(["headline","u0","u1","u2"])}
with ThreadPoolExecutor(3) as pool:
    assets.update({"l"+str(i):v for i,v in enumerate(pool.map(photo,range(3)))})
with sync_playwright() as p:
    b=p.chromium.launch(args=["--allow-file-access-from-files"]);page=b.new_page()
    page.goto(Path("index.html").resolve().as_uri())
    uri=page.evaluate("""async ([d,a])=>{data=d;await document.fonts.ready;await globe.decode();await Promise.all(Object.entries(a).map(async ([k,s])=>{let im=new Image();im.src=s;await im.decode();images[k]=im}));if(!render())throw Error(document.querySelector('#errors').textContent);return C.toDataURL('image/png')}""",[D,assets])
    raw=base64.b64decode(uri.split(",")[1]);im=Image.open(io.BytesIO(raw));assert im.size==(941,1671);im.verify()
    (out/"issue-012.png").write_bytes(raw);(out/"洪滨每日AI早报.png").write_bytes(raw)
    (out/"daily.json").write_text(json.dumps(D,ensure_ascii=False,indent=2));b.close()

# Persist the edition on its own branch before the default workflow's main-branch sync.
import subprocess
def git(*args):
    subprocess.run(["git",*args],check=True)
git("config","user.name","github-actions[bot]")
git("config","user.email","41898282+github-actions[bot]@users.noreply.github.com")
git("add","output/issue-012.png","output/洪滨每日AI早报.png","output/daily.json")
if subprocess.run(["git","diff","--cached","--quiet"]).returncode:
    git("commit","-m","Save reviewed issue 012 PNG")
    git("push","origin","HEAD:refs/heads/editions/012-selected-20261125")
