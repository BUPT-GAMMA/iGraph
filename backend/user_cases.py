"""User-facing questions for the bundled example graphs."""
USER_CASES = [
    {"id": "finance", "name": "金融股权示例", "filename": "finance-ownership.npz",
     "description": "三张金融表已构成股权图，可直接检查股东、控制链和集团关系。",
     "questions": ["请检查这张金融股权图的数据规模、持股方向和数据质量问题。", "请计算 SYN-ENT-000040 的第一大股东，并区分直接持股和累计经济权益。", "请分析 SYN-ENT-000001 的严格控制集团，列出成员、控制链和原始持股证据。"]},
    {"id": "tianlongbabu", "name": "天龙八部人物关系示例", "filename": "tianlongbabu-characters.npz",
     "description": "完整小说经 deepseek-v4-pro 构成人物关系图，可直接探索人物联系和网络结构。",
     "questions": ["请先查找萧峰、乔峰、段誉和虚竹对应的节点，再展示他们的直接人物关系；别名需按图中证据区分。", "请找出人物关系图中连接最多的十个人物，说明计算方法并展示相关子图。", "请对人物关系图进行社区检测，概括各社区的代表人物与相互联系，只依据图中关系作答。"]},
]
