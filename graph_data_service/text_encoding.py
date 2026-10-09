"""Decode uploaded Chinese text without dropping or replacing source bytes."""
import codecs


def decode_txt(data: bytes, *, partial: bool = False) -> str:
    encodings = ("utf-16",) if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            return codecs.getincrementaldecoder(encoding)().decode(data, final=not partial)
        except UnicodeDecodeError:
            continue
    raise ValueError("TXT 文件须使用 UTF-8、GB18030/GBK 或带 BOM 的 UTF-16 编码。")
