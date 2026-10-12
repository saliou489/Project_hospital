"""En-têtes de sécurité HTTP (ENF-06) ajoutés à toutes les réponses de l'API.

- ``X-Content-Type-Options: nosniff`` : le navigateur n'interprète pas un contenu autrement que déclaré ;
- ``X-Frame-Options: DENY`` et ``frame-ancestors 'none'`` : l'API ne peut pas être affichée dans un cadre (clickjacking) ;
- ``Content-Security-Policy: default-src 'none'`` : une réponse de l'API ne peut charger ni exécuter aucune ressource.
  La documentation interactive (/docs, /redoc) charge ses propres scripts : elle est exclue de cette politique ;
- ``Cache-Control: no-store`` : aucune donnée de rendez-vous n'est conservée dans un cache du navigateur ou d'un relais ;
- ``Referrer-Policy`` et ``Permissions-Policy`` : rien n'est divulgué ni demandé au navigateur ;
- ``Strict-Transport-Security`` : seulement lorsque la requête arrive en HTTPS (ou si MEDIRDV_HTTPS=oui),
  pour ne pas enfermer un poste de développement en HTTP sur un HTTPS inexistant.
"""
import os

from starlette.requests import Request
from starlette.responses import Response

CHEMINS_DOCUMENTATION = ("/docs", "/redoc", "/openapi.json")

EN_TETES_FIXES = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
POLITIQUE_API = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def _https(requete: Request) -> bool:
    return os.getenv("MEDIRDV_HTTPS", "").lower() in ("oui", "1", "true") or requete.headers.get("x-forwarded-proto", "") == "https"


async def ajouter_entetes(requete: Request, appeler_suite) -> Response:
    reponse = await appeler_suite(requete)
    for nom, valeur in EN_TETES_FIXES.items():
        reponse.headers.setdefault(nom, valeur)
    if not requete.url.path.startswith(CHEMINS_DOCUMENTATION):
        reponse.headers.setdefault("Content-Security-Policy", POLITIQUE_API)
    if _https(requete):
        reponse.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return reponse
