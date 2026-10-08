"""Render an original 30s AH Sniper motion promo and synthesized soundtrack.

Usage: python tools/create_promo.py --out <folder>
Dependencies: pillow, numpy, imageio-ffmpeg. No network, API keys or stock music.
"""
import argparse
from functools import lru_cache
from pathlib import Path
import math
import os
import subprocess
import wave
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import imageio_ffmpeg

ROOT=Path(__file__).resolve().parents[1]
W,H,FPS=1920,1080,30
GOLD='#ffd66b';WHITE='#f4f2ec';MUTED='#909baa';GREEN='#64e8ad';PURPLE='#c3a0ff'
FONTDIR=Path('C:/Windows/Fonts')

@lru_cache(None)
def font(size,bold=False):
    return ImageFont.truetype(str(FONTDIR/('segoeuib.ttf' if bold else 'segoeui.ttf')),size)

def ease(x):
    return 1-(1-max(0,min(1,x)))**3

def text(im,xy,value,size=32,color=WHITE,bold=False,anchor=None):
    ImageDraw.Draw(im).text(xy,value,font=font(size,bold),fill=color,anchor=anchor,spacing=8)

def panel(im,box,fill='#121a29',outline='#293244',r=22,width=2):
    ImageDraw.Draw(im).rounded_rectangle(tuple(map(int,box)),radius=r,fill=fill,outline=outline,width=width)

def pill(im,x,y,label,color=GOLD,size=24):
    width=int(font(size,True).getlength(label))+36
    panel(im,(x,y,x+width,y+48),fill='#17202d',outline=color,r=12,width=1)
    text(im,(x+18,y+7),label,size,color,True)
    return width

def reticle(im,x,y,r,t):
    d=ImageDraw.Draw(im)
    d.ellipse((x-r,y-r,x+r,y+r),outline='#3d382b',width=2)
    d.arc((x-r,y-r,x+r,y+r),t*30,t*30+105,fill=GOLD,width=5)
    for angle in [0,90,180,270]:
        a=math.radians(angle);c,s=math.cos(a),math.sin(a)
        d.line((x+c*(r-18),y+s*(r-18),x+c*(r+18),y+s*(r+18)),fill=GOLD,width=3)

@lru_cache(None)
def logo(size):
    return Image.open(ROOT/'static/logos/icon-512.png').convert('RGBA').resize((size,size),Image.Resampling.LANCZOS)

def background():
    y,x=np.mgrid[:H,:W]
    glow=np.exp(-(((x-1450)/690)**2+((y-430)/650)**2))
    blue=np.exp(-(((x-200)/1000)**2+((y-950)/400)**2))
    rgb=np.stack((9+glow*12,13+glow*10+blue*6,22+glow*12+blue*15),axis=-1).astype('uint8')
    im=Image.fromarray(rgb).convert('RGBA')
    grid=Image.new('RGBA',(W,H));d=ImageDraw.Draw(grid)
    for x in range(0,W,80):d.line((x,0,x,H),fill=(80,104,150,12))
    for y in range(0,H,80):d.line((0,y,W,y),fill=(80,104,150,12))
    im.alpha_composite(grid)
    return im

BG=background()

def chrome(im,number,t):
    im.alpha_composite(logo(54),(92,58))
    text(im,(164,65),'AH SNIPER',27,WHITE,True)
    text(im,(1824,72),'WORLD OF WARCRAFT  /  EU',19,MUTED,anchor='ra')
    d=ImageDraw.Draw(im);d.line((92,978,1828,978),fill='#263141',width=1)
    text(im,(92,1003),os.getenv("PROMO_SITE_LABEL", "AH SNIPER"),20,GOLD,True)
    text(im,(1828,1003),f'0{number+1} / 06',20,MUTED,anchor='ra')
    for i in range(6):
        x=800+i*58;d.rounded_rectangle((x,1009,x+38,1014),radius=2,fill=GOLD if i==number else '#293344')
    # Quiet moving stars: controlled motion keeps the UI and type legible.
    for i in range(22):
        x=(i*173+38)%W;y=(i*97-t*(7+i%3))%H
        if 120<y<940:d.ellipse((x,y,x+2,y+2),fill='#69614a')

def title(im,kicker,lines,u,size=88):
    dy=int(34*(1-ease(u/0.7)))
    text(im,(96,180+dy),kicker,22,GOLD,True)
    for i,line in enumerate(lines):text(im,(92,226+dy+i*(size+12)),line,size,WHITE,True)

def scene(n,u,t):
    im=BG.copy();d=ImageDraw.Draw(im);chrome(im,n,t)
    if n==0:
        title(im,'АУКЦИОН WORLD OF WARCRAFT',['Хороший лот.','Твой точный','выстрел.'],u,96)
        text(im,(98,640),'Находи выгодные предложения',33,MUTED)
        text(im,(98,687),'с AH Sniper.',33,MUTED)
        pill(im,98,784,'BoE',GOLD);pill(im,210,784,'Скидки',GREEN);pill(im,365,784,'Реалмы',PURPLE)
        reticle(im,1415,525,278,t)
        size=int(430+22*ease(u));im.alpha_composite(logo(size),(1415-size//2,525-size//2))
    elif n==1:
        title(im,'РЫНОК ПЕРЕД ГЛАЗАМИ',['92 EU-реалма.','Один экран.'],u,86)
        text(im,(98,500),'Сравнивай цены между серверами.',31,MUTED)
        text(im,(98,548),'Замечай разницу.',31,MUTED)
        count=round(92*ease(u/1.4));text(im,(98,658),str(count),170,GOLD,True)
        text(im,(332,784),'реалма EU',32,MUTED)
        cx,cy=1390,537
        for ring in [155,265,355]:d.ellipse((cx-ring,cy-ring,cx+ring,cy+ring),outline='#293342',width=2)
        for i in range(32):
            a=i*2.399+0.02*t;r=105+((i*71)%230);x=cx+math.cos(a)*r;y=cy+math.sin(a)*r
            if u>i*0.025:
                d.line((cx,cy,x,y),fill='#313b41',width=1)
                d.ellipse((x-4,y-4,x+4,y+4),fill=GOLD if i%4==0 else GREEN)
        for x,y,label in [(1090,325,'Kazzak'),(1490,330,'Draenor'),(1415,747,'Tarren Mill')]:
            panel(im,(x,y,x+215,y+55));text(im,(x+18,y+9),label,27,WHITE,True)
        im.alpha_composite(logo(112),(cx-56,cy-56));reticle(im,cx,cy,88,t)
    elif n==2:
        title(im,'СНАЙПИНГ',['Скидка видна сразу.'],u,76)
        text(im,(98,347),'Цена, ilvl и характеристики — в одной строке.',30,MUTED)
        x=96;y=440+int(30*(1-ease(u)))
        panel(im,(x,y,1824,876),fill='#101827')
        text(im,(126,y+28),'Снайпинг',28,GOLD,True)
        pill(im,1380,y+20,'Мин. скидка 40%',GREEN,22)
        for xx,label in [(130,'ПРЕДМЕТ'),(866,'ILVL'),(1028,'ЦЕНА'),(1260,'СТАТЫ / СОКЕТ'),(1672,'СКИДКА')]:text(im,(xx,y+104),label,18,MUTED,True)
        rows=[("Fanged Brute’s Greatbelt",'321','1 999 999g','Vers + Mastery / сокет','−50%'),
              ("Bound Serpent’s Jade Eye",'321','3 999 999g','Haste + Mastery / сокет','−43%'),
              ("Greaves of Noxious Depths",'324','1 380 000g','Haste + Vers','−41%')]
        for i,row in enumerate(rows):
            yy=y+153+i*81
            panel(im,(115,yy-7,1804,yy+65),fill='#1c2831' if i==0 else '#141d2c',outline=GOLD if i==0 else '#233044',r=10,width=1)
            for xx,value,col,size in zip([133,881,1028,1260,1672],row,[PURPLE,WHITE,GOLD,MUTED,GREEN],[27,26,26,24,31]):text(im,(xx,yy+8),value,size,col,i!=3)
        text(im,(100,911),'Иллюстрация интерфейса · цены показаны как пример',19,MUTED)
    elif n==3:
        title(im,'ТОЧНЫЙ ПОИСК BOE',['Твои статы.','Твой сокет.'],u,86)
        text(im,(98,519),'Отдельный фильтр для доп. стата.',29,MUTED)
        pill(im,98,606,'Haste + Mastery',PURPLE,25)
        pill(im,98,674,'С сокетом',GOLD,25)
        pill(im,98,742,'Avoidance',GREEN,25)
        x=962;y=204
        panel(im,(x,y,1808,890),fill='#141c2b',outline='#475042',r=30)
        pill(im,x+42,y+36,'BoE / ilvl 321',GOLD,24)
        text(im,(x+42,y+128),'Bound Serpent’s',43,PURPLE,True)
        text(im,(x+42,y+184),'Jade Eye',43,PURPLE,True)
        d.line((x+42,y+274,1766,y+274),fill='#303c4c',width=2)
        options=[('Haste + Mastery',PURPLE),('1 сокет',GOLD),('Avoidance',GREEN)]
        for i,(label,color) in enumerate(options):
            yy=y+319+i*74;active=u>0.7+i*0.55
            d.ellipse((x+44,yy+9,x+60,yy+25),fill=color if active else '#3d4756')
            text(im,(x+84,yy),label,31,color if active else MUTED,True)
            if active:d.line((1708,yy+20,1718,yy+30,1740,yy+6),fill=GREEN,width=4)
        text(im,(x+42,y+568),'4 000 000g',43,GOLD,True)
        text(im,(1764,y+593),'Kazzak',25,MUTED,anchor='ra')
        text(im,(966,919),'Пример выбранного варианта предмета',19,MUTED)
    elif n==4:
        title(im,'ОТ СДЕЛКИ К ДЕТАЛЯМ',['Клик по цене. Вся картина.'],u,69)
        text(im,(98,341),'Открывай предмет и сравнивай подходящие лоты.',30,MUTED)
        panel(im,(96,444,667,843));text(im,(130,478),'СНАЙПИНГ',21,MUTED,True)
        text(im,(130,542),'Fanged Brute’s',36,PURPLE,True);text(im,(130,589),'Greatbelt',36,PURPLE,True)
        panel(im,(129,685,630,775),fill='#2b2a20',outline=GOLD,r=16)
        text(im,(160,700),'1 999 999g  →',43,GOLD,True)
        # The animated cursor lands on the price, then the matching realm glows.
        progress=ease((u-.2)/1.1);cx=814-270*progress;cy=855-115*progress
        d.polygon([(cx,cy),(cx+4,cy+38),(cx+14,cy+26),(cx+28,cy+25)],fill=WHITE,outline='#070d16')
        for xx in range(706,850,25):d.line((xx,645,xx+12,645),fill=GOLD,width=3)
        panel(im,(889,444,1824,843));text(im,(928,477),'БРАУЗЕР / КАРТОЧКА ПРЕДМЕТА',21,MUTED,True)
        pill(im,928,535,'Vers + Mastery',PURPLE,23);pill(im,1181,535,'С сокетом',GOLD,23)
        for i,(realm,price) in enumerate([('Kazzak','1 999 999g'),('Draenor','2 150 000g'),('Tarren Mill','2 390 000g')]):
            yy=616+i*64
            if i==0 and u>1.4:panel(im,(918,yy-3,1798,yy+50),fill='#243329',outline=GREEN,r=9,width=1)
            text(im,(936,yy+6),realm,28,WHITE,True);text(im,(1772,yy+6),price,28,GOLD,True,anchor='ra')
        text(im,(100,911),'Иллюстрация интерфейса · цены показаны как пример',19,MUTED)
    else:
        reticle(im,960,345,147,t);im.alpha_composite(logo(234),(843,228))
        text(im,(960,513),'AH SNIPER',110,WHITE,True,anchor='mt')
        text(im,(960,659),'Меньше поиска. Больше возможностей.',38,MUTED,anchor='mt')
        panel(im,(649,772,1271,862),fill=GOLD,outline=GOLD,r=18)
        text(im,(960,781),'example.invalid  →',45,'#111722',True,anchor='mt')
    return im

def soundtrack(path):
    sr=44100;length=30;n=sr*length;mix=np.zeros((n,2),dtype=np.float64);rng=np.random.default_rng(71)
    def add(at,sig,gain=.2,pan=0):
        start=int(at*sr);end=min(n,start+len(sig))
        if end<=start:return
        mono=sig[:end-start]*gain
        mix[start:end,0]+=mono*math.sqrt((1-pan)/2);mix[start:end,1]+=mono*math.sqrt((1+pan)/2)
    def note(midi,dur,kind='pluck'):
        t=np.arange(int(sr*dur))/sr;f=440*2**((midi-69)/12)
        if kind=='pad':
            sig=sum(np.sin(2*np.pi*f*(1+det)*t)*.33 for det in [-.0015,0,.0015])
            env=np.minimum(1,t/.3)*np.minimum(1,(dur-t)/.7)
        elif kind=='bass':sig=np.sin(2*np.pi*f*t)+.23*np.sin(4*np.pi*f*t);env=np.minimum(1,t/.008)*np.exp(-t*4)
        else:
            sig=np.sin(2*np.pi*f*t)+.24*np.sin(4*np.pi*f*t)+.10*np.sin(6*np.pi*f*t)
            env=np.minimum(1,t/.006)*np.exp(-t*7)
        return sig*env
    chords=[[48,51,55,62],[44,48,51,58],[46,50,53,60],[43,46,50,57]]
    for bar in range(15):
        at=bar*2;chord=chords[(bar//2)%4]
        for midi in chord:add(at,note(midi,2.65,'pad'),.075,pan=(midi%3-1)*.45)
        for step in range(8):
            at=bar*2+step*.25;mid=chord[[0,2,1,3,2,1,3,2][step]]+24
            sig=note(mid,.65);gain=.10 if 4<=at<26 else .048
            add(at,sig,gain,pan=(-1 if step%2 else 1)*.45);add(at+.375,sig,gain*.28,pan=(1 if step%2 else -1)*.6)
    for beat in range(8,54):
        at=beat*.5
        t=np.arange(int(sr*.32))/sr
        kick=np.sin(2*np.pi*(48*t+6*(1-np.exp(-t*25))))*np.exp(-t*14)
        add(at,kick,.52)
        add(at,note(chords[(int(at)//4)%4][0]-12,.45,'bass'),.22)
        if beat%2:
            t=np.arange(int(sr*.19))/sr;noise=rng.standard_normal(len(t));noise=np.concatenate(([0],np.diff(noise)))*.4
            add(at,noise*np.exp(-t*27)+np.sin(2*np.pi*180*t)*np.exp(-t*32)*.3,.19)
        for off in [0,.25]:
            t=np.arange(int(sr*.07))/sr;noise=rng.standard_normal(len(t));hp=np.concatenate(([0],np.diff(noise)))
            add(at+off,hp*np.exp(-t*65),.033,pan=.35 if off else -.35)
    # Transition swells and a soft low impact, all synthesized from a seeded noise source.
    for at in [4,9,15,21,26]:
        t=np.arange(int(sr*.6))/sr;noise=rng.standard_normal(len(t))
        smooth=np.convolve(noise,np.ones(22)/22,mode='same')
        add(at-.6,smooth*(t/.6)**2,.20,pan=-.2)
        t=np.arange(sr)/sr;add(at,np.sin(2*np.pi*43*t)*np.exp(-t*7),.30)
    # Gentle stereo echo; master fade, soft saturation and bounded peak.
    timeline=np.arange(n)/sr
    mix*= (np.minimum(1,timeline/.25)*np.minimum(1,(30-timeline)/1.3))[:,None]
    mix=np.tanh(mix*1.35);mix*=.88/max(np.max(np.abs(mix)),.01)
    with wave.open(str(path),'wb') as f:
        f.setnchannels(2);f.setsampwidth(2);f.setframerate(sr);f.writeframes((mix*32767).astype('<i2').tobytes())

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);parser.add_argument('--preview',action='store_true');args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    cuts=[0,4,9,15,21,26,30]
    thumbs=[]
    for n in range(6):
        im=scene(n,2,cuts[n]+2).convert('RGB');im.save(args.out/f'scene-{n+1}.jpg',quality=94)
        thumbs.append(im.resize((640,360),Image.Resampling.LANCZOS))
    sheet=Image.new('RGB',(1280,1080))
    for i,im in enumerate(thumbs):sheet.paste(im,((i%2)*640,(i//2)*360))
    sheet.save(args.out/'storyboard.jpg',quality=93)
    if args.preview:return
    soundtrack(args.out/'AH_Sniper_original_music.wav')
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    cmd=[ffmpeg,'-y','-loglevel','warning','-f','rawvideo','-vcodec','rawvideo','-pix_fmt','rgb24','-s',f'{W}x{H}','-r',str(FPS),'-i','-',
         '-i',str(args.out/'AH_Sniper_original_music.wav'),'-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-b:a','256k','-t','30','-movflags','+faststart',str(args.out/'AH_Sniper_30s.mp4')]
    with subprocess.Popen(cmd,stdin=subprocess.PIPE) as proc:
        for frame in range(30*FPS):
            t=frame/FPS;n=max(i for i in range(6) if t>=cuts[i]);u=t-cuts[n]
            im=scene(n,u,t)
            if n and u<.35:
                prev=scene(n-1,cuts[n]-cuts[n-1]+u,t)
                im=Image.blend(prev,im,ease(u/.35))
            fade=min(1,t/.3,(30-t)/.45)
            if fade<1:im=Image.blend(Image.new('RGBA',(W,H),'#090d16'),im,max(0,fade))
            proc.stdin.write(im.convert('RGB').tobytes())
            if frame%150==0:print(f'Render {frame}/{30*FPS}',flush=True)
        proc.stdin.close();code=proc.wait()
        if code:raise RuntimeError(f'ffmpeg exited {code}')
    print(args.out/'AH_Sniper_30s.mp4')

if __name__=='__main__':main()
