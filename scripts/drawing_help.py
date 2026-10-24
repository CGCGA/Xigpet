"""Drawing instructions shared by the chat agent and idle creation."""

DRAWING_HELP = """通过 draw_picture 创作程序绘图。传 title（给作品取名）、code、可选 keywords（主题标签）、mood（情绪标签）、width/height（256–1024，默认768）。
code 是受限 Python 绘图语句，不需要 import，不要创建 Image 或保存文件。程序以2倍分辨率抗锯齿绘制，自动保存 PNG 与可编辑图形源文件。
已有 W、H、pi、tau；支持变量、四则运算、if、for/range（单次最多500项）、列表/元组、列表.append、下标和简单 f-string。
可调用 draw.rectangle、draw.rounded_rectangle、draw.ellipse、draw.polygon、draw.line、draw.arc、draw.pieslice、draw.point，参数与 Pillow 对应；draw.text((x,y), '文字', fill='颜色', font_size=24, anchor='mm')。
所有图形可带 id='eye_left' 和 opacity=0.5；ID必须唯一，不传则自动编号。颜色支持 #RRGGBB、#RRGGBBAA 或 RGB/RGBA 元组。
曲线路径：draw.path([('M',x,y),('Q',cx,cy,x,y),('C',c1x,c1y,c2x,c2y,x,y),('L',x,y),('Z',)], fill='颜色', outline='颜色', width=3, id='hair')。仅绝对坐标，每条路径最多128条指令；各子路径分别填充，不支持镂空填充规则。
渐变：draw.gradient((0,0,W,H), colors=['#c5e7ff','#fff3dc'], direction='vertical', id='sky')；direction为vertical/horizontal，shape可为rectangle/rounded_rectangle/ellipse，支持radius。
layer('shadow', opacity=0.4) 开始或选择图层，end_layer() 返回base；最多16层，按首次出现的顺序叠加。opacity作用于整个图层，适合阴影和高光。
background('颜色') 设置整幅画的底色，也可 background('transparent') 输出透明背景。尽量在开头调用。支持 sin/cos/sqrt/radians（也可 math.sin 等）、randint/uniform（也可 random.uniform）、min/max/abs/round/int/float/str/len。
没有 import、while、函数/类定义、文件、网络、进程、任意属性访问。坐标数值不得超过1000000；最多2500个图形、30000步计算，超限会失败。
先安排主体、前后层次和配色，再用曲线画轮廓，少量透明色增加阴影；避免用大量小圆堆成所有细节。
示例：
draw.gradient((0,0,W,H), colors=['#c5e7ff','#fff3dc'], id='sky')
draw.path([('M',W*.25,H*.6),('C',W*.1,H*.1,W*.9,H*.1,W*.75,H*.6),('Z',)],fill='#9ac9ef',outline='#529bd3',width=4,id='body')
layer('shine', opacity=0.35)
draw.ellipse((W*.3,H*.25,W*.45,H*.4),fill='white',id='highlight')
end_layer()
draw.text((W/2,H*.83),'送给你',fill='#29485e',font_size=32,anchor='mm',id='caption')
修改旧画：先search_media找文件，再read_drawing(filename, ids?, offset?, limit?)读取图形与revision；edit_drawing(filename, expect_revision, ops, title?)只改指定图形，自动发送新版本，原作保留。
ops支持：{op:'set',id:'body',style:{fill:'#ffccaa'}}、{op:'move',id:'body',dx:10,dy:0}、{op:'set',id:'body',args:[新的坐标或路径]}、{op:'remove',id:'highlight'}、{op:'background',color:'#fff'}、{op:'layer',id:'shine',opacity:0.2,visible:true}、{op:'order',id:'caption',position:'front'}、{op:'add',id:'star',kind:'ellipse',args:[[20,20,40,40]],style:{fill:'gold'},layer:'base'}。
set的style与原样式合并，fill/outline传null表示不填充/不描边。图形顺序在所属图层内生效，图层顺序优先于图形顺序。read_drawing返回有限数量图形，用offset翻页或ids读取目标；每次修改最多100个ops。旧PNG没有源数据时会如实报错。
成功后自然说出画了什么，图片由程序单独发送；失败时根据错误修改，不能声称已经画好。"""
