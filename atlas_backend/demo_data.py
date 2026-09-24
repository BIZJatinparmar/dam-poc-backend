"""Demo user profiles and decorative artwork."""


USERS = [
    dict(id="maya", name="Maya Chen", email="maya@northstar.example", role="admin", team="Marketing", active=True, initials="MC"),
    dict(id="alex", name="Alex Morgan", email="alex@northstar.example", role="editor", team="Marketing", active=True, initials="AM"),
    dict(id="quinn", name="Quinn Rivera", email="quinn@studio.example", role="agency", team="External agency", active=True, initials="QR"),
]


PALETTE = {
    "coast": ("#a9d6d9", "#f6d8ad", "#287e8c"),
    "mountain": ("#c4c7dc", "#f1cfb0", "#5a718b"),
    "city": ("#393f61", "#e2a883", "#202b44"),
    "product": ("#dfae85", "#f8e4c7", "#9f583f"),
    "desert": ("#e5a477", "#f4d8ad", "#b15c48"),
    "people": ("#bfd0c4", "#e8dcbf", "#617d71"),
    "forest": ("#718e70", "#d3d6a6", "#385a4e"),
    "studio": ("#837a82", "#d9b1a6", "#46424e"),
}


def art_svg(theme: str) -> str:
    sky, light, dark = PALETTE.get(theme, PALETTE["coast"])
    if theme == "product":
        subject = '<ellipse cx="400" cy="455" rx="190" ry="29" fill="#603d34" opacity=".17"/><rect x="315" y="171" width="170" height="270" rx="22" fill="#d4a16f"/><rect x="329" y="183" width="142" height="242" rx="14" fill="#f2d8ac"/><rect x="348" y="272" width="104" height="68" rx="3" fill="#ead5bd"/><circle cx="400" cy="304" r="19" fill="#bb8568"/><rect x="350" y="140" width="100" height="48" rx="7" fill="#683c31"/>'
    elif theme in ("city", "studio"):
        subject = f'<path d="M0 410 L90 410 90 220 190 220 190 358 252 358 252 150 370 150 370 390 430 390 430 200 545 200 545 350 632 350 632 250 800 250 800 550 0 550Z" fill="{dark}" opacity=".86"/><path d="M0 455 Q180 410 360 453 T800 432 L800 550 0 550Z" fill="#1c2638" opacity=".4"/>'
    elif theme == "people":
        subject = f'<path d="M0 400 Q200 295 400 395 T800 355 L800 550 0 550Z" fill="{dark}" opacity=".65"/><circle cx="318" cy="268" r="30" fill="#594c48"/><path d="M285 308 Q320 290 351 311 L367 466 272 466Z" fill="#e9d7bb"/><circle cx="474" cy="256" r="27" fill="#594c48"/><path d="M442 294 Q475 279 505 300 L521 459 429 459Z" fill="#c4775d"/>'
    elif theme == "forest":
        subject = ''.join(f'<path d="M{x} 550 L{x-12} 550 {x-9} 275 {x+8} 275 {x+17} 550Z" fill="#344d43"/><circle cx="{x}" cy="{220+(x%3)*28}" r="{98+(x%4)*12}" fill="{dark}" opacity=".65"/>' for x in (100, 270, 500, 710))
    else:
        subject = f'<path d="M0 390 Q170 260 340 377 T800 280 L800 550 0 550Z" fill="{dark}" opacity=".72"/><path d="M0 466 Q170 360 360 456 T800 398 L800 550 0 550Z" fill="{dark}" opacity=".55"/>'
        if theme == "coast":
            subject += '<path d="M0 415 Q200 455 390 426 T800 450 L800 550 0 550Z" fill="#6caeb4" opacity=".82"/>'
        if theme == "desert":
            subject += '<path d="M305 550 L431 359 476 359 595 550Z" fill="#f0cda3" opacity=".8"/>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="550" viewBox="0 0 800 550"><defs><linearGradient id="sky" x2="0" y2="1"><stop stop-color="{sky}"/><stop offset="1" stop-color="{light}"/></linearGradient></defs><rect width="800" height="550" fill="url(#sky)"/><circle cx="608" cy="146" r="69" fill="#fff4d9" opacity=".7"/>{subject}</svg>'
