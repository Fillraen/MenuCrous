"""Signatures manuscrites des utilisateurs SoWeSign.

Chaque utilisateur fournit SA signature : dessinée sur la page web du bot (même cadre que
SoWeSign) ou envoyée en photo (papier blanc, stylo foncé). On la ramène au format du cadre de
l'appli : PNG transparent, trait noir, ratio 3:2 (toDataURL() du canvas SoWeSign).
"""
from __future__ import annotations

import base64
import io
import random

from PIL import Image, ImageFilter, ImageOps

LARGEUR, HAUTEUR = 408, 272  # cadre SoWeSign sur ordinateur (hauteur = 2/3 de la largeur)
MARGE = 18
MAX_SIGNATURES = 5


class SignatureInvalide(ValueError):
    pass


def depuis_image(donnees: bytes) -> str:
    """Photo / image quelconque → data URL PNG au format SoWeSign."""
    try:
        img = Image.open(io.BytesIO(donnees))
        img = ImageOps.exif_transpose(img)
    except Exception as exc:
        raise SignatureInvalide("Image illisible") from exc
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        fond = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(fond, img.convert("RGBA"))
    gris = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    if max(gris.size) > 1600:
        gris.thumbnail((1600, 1600))
    # Encre = pixels nettement plus sombres que le papier (seuil adaptatif simple)
    hist = gris.histogram()
    total = sum(hist)
    cumul, mediane = 0, 255
    for niveau, n in enumerate(hist):
        cumul += n
        if cumul >= total / 2:
            mediane = niveau
            break
    seuil = max(60, mediane - 70)
    encre = gris.point(lambda p: 255 if p < seuil else 0).filter(ImageFilter.MedianFilter(3))
    boite = encre.getbbox()
    if not boite:
        raise SignatureInvalide("Aucun trait détecté : signe en foncé sur fond clair")
    encre = encre.crop(boite)
    if encre.width < 20 or encre.height < 8:
        raise SignatureInvalide("Trait trop petit")
    return _cadrer(encre)


def depuis_dataurl(dataurl: str) -> str:
    """Dessin fait sur la page web (déjà noir sur transparent) → recadré et centré."""
    if not dataurl.startswith("data:image/png;base64,"):
        raise SignatureInvalide("Format inattendu")
    img = Image.open(io.BytesIO(base64.b64decode(dataurl.split(",", 1)[1]))).convert("RGBA")
    alpha = img.getchannel("A")
    boite = alpha.getbbox()
    if not boite:
        raise SignatureInvalide("Cadre vide")
    encre = alpha.crop(boite)
    if encre.width < 30 or encre.height < 10:
        raise SignatureInvalide("Signature trop petite")
    return _cadrer(encre)


def _cadrer(encre: Image.Image) -> str:
    """Masque d'encre (blanc = trait) → trait noir centré dans le cadre transparent."""
    l, h = LARGEUR - 2 * MARGE, HAUTEUR - 2 * MARGE
    r = min(l / encre.width, h / encre.height)
    encre = encre.resize((max(1, int(encre.width * r)), max(1, int(encre.height * r))), Image.LANCZOS)
    cadre = Image.new("RGBA", (LARGEUR, HAUTEUR), (0, 0, 0, 0))
    noir = Image.new("RGBA", encre.size, (0, 0, 0, 255))
    cadre.paste(noir, ((LARGEUR - encre.width) // 2, (HAUTEUR - encre.height) // 2), encre)
    buf = io.BytesIO()
    cadre.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def apercu(dataurl: str) -> bytes:
    """PNG sur fond blanc (pour l'aperçu Discord, lisible en thème sombre)."""
    img = Image.open(io.BytesIO(base64.b64decode(dataurl.split(",", 1)[1]))).convert("RGBA")
    fond = Image.new("RGBA", img.size, (255, 255, 255, 255))
    buf = io.BytesIO()
    Image.alpha_composite(fond, img).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def au_hasard(signatures: list[str]) -> str:
    if not signatures:
        raise SignatureInvalide("Aucune signature enregistrée : `/sws signature`")
    return random.choice(signatures)


PAGE_DESSIN = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>MenuCrous — ma signature</title>
<style>
body{{margin:0;font:16px system-ui,sans-serif;background:#1e1f22;color:#eee;display:flex;flex-direction:column;align-items:center;padding:20px}}
h1{{font-size:1.2rem}} p{{color:#aaa;max-width:420px;text-align:center}}
canvas{{background:#fff;border-radius:10px;touch-action:none;width:min(92vw,408px);aspect-ratio:3/2}}
.b{{display:flex;gap:10px;margin-top:14px}} button{{font:inherit;padding:10px 18px;border-radius:8px;border:0;cursor:pointer}}
#ok{{background:#5865f2;color:#fff}} #eff{{background:#444;color:#eee}} #msg{{margin-top:14px;min-height:1.4em}}
</style></head><body>
<h1>✍️ Ta signature SoWeSign</h1>
<p>Signe comme d'habitude dans le cadre. Tu peux en enregistrer plusieurs (jusqu'à {max}) :
le bot en choisit une au hasard à chaque signature. Ce lien est personnel et expire dans 15 min.</p>
<canvas id="c" width="408" height="272"></canvas>
<div class="b"><button id="eff">Effacer</button><button id="ok">Enregistrer</button></div>
<div id="msg"></div>
<script>
const c=document.getElementById('c'),x=c.getContext('2d');let d=false,p=null;
x.lineWidth=4;x.lineCap='round';x.lineJoin='round';x.strokeStyle='#000';
const pos=e=>{{const r=c.getBoundingClientRect();return [(e.clientX-r.left)*c.width/r.width,(e.clientY-r.top)*c.height/r.height]}};
c.onpointerdown=e=>{{d=true;p=pos(e);c.setPointerCapture(e.pointerId)}};
c.onpointermove=e=>{{if(!d)return;const q=pos(e);x.beginPath();x.moveTo(...p);x.lineTo(...q);x.stroke();p=q}};
c.onpointerup=c.onpointercancel=()=>{{d=false}};
document.getElementById('eff').onclick=()=>x.clearRect(0,0,c.width,c.height);
document.getElementById('ok').onclick=async()=>{{
  const m=document.getElementById('msg');m.textContent='Envoi…';
  const r=await fetch(location.href,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{signature:c.toDataURL()}})}});
  const t=await r.json();m.textContent=t.message;if(r.ok)x.clearRect(0,0,c.width,c.height);
}};
</script></body></html>"""
