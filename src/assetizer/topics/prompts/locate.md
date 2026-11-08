依次用 view_file 读取下面这张图片，只读一次。禁止其他工具、搜索或 ffmpeg。读完后直接回答，不要再调用任何工具。

{image}

在图里找到：{target}
只输出一行 JSON：{"box": [ymin, xmin, ymax, xmax]}，坐标按图片宽高归一化到 0–1000。找不到输出 {"box": null}。
