"""Test site for the link analyzer (agent.py links, workflow.py links): a fictional Porto bike shop with planted link problems.

One process serves the shop, www.veloribeira.pt, and the fictional "external" sites it links to,
choosing the site by the Host header. 

Setup, once:
    1. Add these lines to /etc/hosts:
           127.0.0.1  www.veloribeira.pt www.douroride.pt www.trailgear-supply.com routes.pedalmap.eu www.portobikefest.pt
           127.0.0.2  www.ciclovia-norte.pt
       Nothing listens on 127.0.0.2, so www.ciclovia-norte.pt refuses connections like a dead server.
    2. Allow port 80 without root (until the next reboot), so URLs don't carry a ":8080":
           sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
       Or pass --port 8080; links between the sites are then rewritten to include the port.

Run:
    uv run testsite/server.py

Pages to analyze, each with its own planted problems:
    http://www.veloribeira.pt/
    http://www.veloribeira.pt/partners
    http://www.veloribeira.pt/blog/douro-valley-3-day-itinerary
"""

import argparse
import mimetypes
import random
import socket
import struct
import time
import zlib
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"

MAIN_HOST = "www.veloribeira.pt"
UNREACHABLE_HOST = "www.ciclovia-norte.pt"  # 127.0.0.2 in /etc/hosts, where nothing listens


@dataclass
class Page:
    template: str  # file under templates/
    title: str = ""
    layout: str | None = None  # template to wrap this one in; None if the template is a whole document
    context: dict[str, str] = field(default_factory=dict)  # values for the template's {{name}} placeholders
    status: int = 200
    head_status: int | None = None  # a different status for HEAD requests, like a misconfigured cache
    delay: float = 0  # seconds to wait before answering, like a hanging backend


@dataclass
class Redirect:
    location: str
    status: int = 301


@dataclass
class File:
    body: bytes
    content_type: str


@dataclass
class Site:
    routes: dict[str, Page | Redirect | File]
    not_found: Page
    catch_all: dict[str, Page] = field(default_factory=dict)  # path prefix -> page for any other path under it
    static: bool = False  # serve the files in static/ under /static/
    latency: tuple[float, float] = (0.04, 0.15)  # seconds, so response times look like a real server's


def page_on(site):
    # pages of one site share its layout, templates/<site>/base.html
    def page(template, title, **kwargs):
        return Page(f"{site}/{template}", title, layout=f"{site}/base.html", **kwargs)
    return page

shop_page = page_on("veloribeira")
douro_page = page_on("douroride")
trailgear_page = page_on("trailgear")
pedalmap_page = page_on("pedalmap")


def solid_png(width, height, color):
    # a one-colour PNG, standing in for a photo
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = (b"\x00" + bytes(color) * width) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def simple_pdf(lines):
    # a one-page PDF with a few lines of ASCII text
    text = " ".join(f"({line}) Tj T*" for line in lines)
    stream = f"BT /F1 12 Tf 16 TL 72 770 Td {text} ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return pdf


IMAGES = {  # path under /static/img/ -> (width, height, colour)
    "favicon.png": (32, 32, (212, 98, 47)),
    "hero-ribeira.png": (1200, 500, (38, 86, 106)),
    "products/velo-urban-3.png": (600, 400, (60, 96, 128)),
    "products/trail-5-hardtail.png": (600, 400, (84, 110, 64)),
    "products/gravel-x2.png": (600, 400, (150, 120, 80)),  # the homepage asks for gravel-x2.jpg
    "products/folding-city-16.png": (600, 400, (170, 70, 60)),
    "products/explorer-20-kids.png": (600, 400, (230, 170, 60)),
    "products/commuter-helmet.png": (600, 400, (70, 70, 80)),
    "products/cargo-duo.png": (600, 400, (40, 120, 110)),
    "tours/coastal-ride.png": (600, 400, (90, 150, 180)),
    "tours/geres.png": (600, 400, (50, 95, 60)),
    "tours/aveiro.png": (600, 400, (210, 190, 150)),
    "blog/douro-valley.png": (900, 500, (110, 130, 60)),
}

IN_STOCK = {"stock": "In stock at our Ribeira shop", "stock_class": "", "cta": "Reserve for collection"}

PRODUCTS = {
    "velo-urban-3": {
        "name": "Velo Urban 3",
        "price": "€749",
        "summary": "Our best-selling city bike: a 3-speed hub, dynamo lights and a front basket that takes a full "
                   "shopping bag. Built for cobbles and the climb up from the river.",
        "specs": "<li>Frame: aluminium, step-through</li><li>Gears: 3-speed internal hub</li>"
                 "<li>Brakes: hydraulic disc</li><li>Weight: 15.8 kg</li>",
    },
    "trail-5-hardtail": {
        "name": "Trail 5 hardtail",
        "price": "€1,290",
        "summary": "A 29-inch hardtail for the forest trails around Gaia and Valongo, with a 120 mm air fork and a "
                   "dropper post as standard.",
        "specs": "<li>Frame: aluminium, 29-inch wheels</li><li>Fork: 120 mm air</li>"
                 "<li>Gears: 1×12</li><li>Weight: 13.4 kg</li>",
    },
    "gravel-x2": {
        "name": "Gravel X2",
        "price": "€1,890",
        "summary": "Fast on tarmac, calm on gravel. Room for 45 mm tyres, mounts for racks and three bottles, and "
                   "the bike our guides ride on the Gerês weekend.",
        "specs": "<li>Frame: carbon, flat-mount disc</li><li>Tyres: 700×40c tubeless</li>"
                 "<li>Gears: 2×11</li><li>Weight: 9.6 kg</li>",
    },
    "folding-city-16": {
        "name": "Folding City 16",
        "price": "€899",
        "summary": "Folds in 15 seconds and fits under a café table. Ideal if you combine riding with the metro or "
                   "the train to the coast.",
        "specs": "<li>Wheels: 16-inch</li><li>Gears: 6-speed</li>"
                 "<li>Folded size: 58 × 54 × 27 cm</li><li>Weight: 11.2 kg</li>",
    },
    "explorer-20-kids": {
        "name": "Explorer 20 kids' bike",
        "price": "€329",
        "summary": "A light 20-inch bike for riders aged 6 to 9, with brake levers sized for small hands and a "
                   "chain guard to keep trousers clean.",
        "specs": "<li>Wheels: 20-inch</li><li>Gears: 7-speed</li>"
                 "<li>Rider height: 115–135 cm</li><li>Weight: 9.1 kg</li>",
    },
    "commuter-helmet": {
        "name": "Commuter helmet with MIPS",
        "price": "€59",
        "stock": "Out of stock. More arriving in November.",
        "stock_class": "out",
        "cta": "Email me when it's back",
        "summary": "A light urban helmet with MIPS protection, a rear LED and a magnetic buckle you can close with "
                   "gloves on.",
        "specs": "<li>Sizes: S (51–55 cm), M (55–59 cm), L (59–62 cm)</li><li>Weight: 390 g (M)</li>"
                 "<li>Certified to EN 1078</li>",
    },
}

PASSEIOS = {
    "vinho-e-bicicleta": {
        "name": "Bicicleta e vinho",
        "duration": "Dia inteiro · 35 km",
        "price": "75 € por pessoa",
        "description": "Saímos do Peso da Régua pela margem sul do rio até ao Pinhão, com visita e prova de vinhos "
                       "em duas quintas e almoço regional numa delas. Regresso de comboio ao fim da tarde. Nível "
                       "fácil a moderado; há bicicletas elétricas disponíveis.",
    },
    "pinhao-ao-por-do-sol": {
        "name": "Pinhão ao pôr do sol",
        "duration": "Fim de tarde · 18 km",
        "price": "45 € por pessoa",
        "description": "Um passeio curto entre as vinhas à volta do Pinhão, a terminar com um copo de vinho do Porto "
                       "no miradouro de Casal de Loivos ao pôr do sol.",
    },
}

PEDALMAP_ROUTES = {
    "51007": {
        "name": "Porto to Matosinhos seaside loop",
        "distance": "24 km",
        "climb": "60 m",
        "description": "From Ribeira along the river to Foz, north along the seafront to Matosinhos, and back "
                       "through Parque da Cidade. Flat, and mostly on cycle paths.",
    },
    "50344": {
        "name": "Parque da Cidade family loop",
        "distance": "9 km",
        "climb": "35 m",
        "description": "Two laps of Porto's biggest park plus the seafront between Castelo do Queijo and Praia do "
                       "Molhe. No traffic and plenty of ice-cream stops.",
    },
    "51122": {
        "name": "Pinhão – São João da Pesqueira – Pinhão",
        "distance": "32 km",
        "climb": "910 m",
        "description": "Day 2 of our Douro Valley itinerary: a long climb to the São Salvador do Mundo viewpoint "
                       "and São João da Pesqueira, then a fast descent back to the river.",
    },
}

shop_routes = {
    # home.html also plants accessibility defects: images whose alt is "Photo", a file name or the link's own text,
    # or missing; a "Find out more" link with nothing around it saying where it goes, and a link whose aria-label is
    # "link"; a Portuguese review with no lang, and Portuguese marked lang="es"; a copy-pasted "Workshop" heading and
    # VAT small print marked up as an h5; an email field with no label. Decoys: "Click here" explained by its
    # sentence, Spanish marked lang="es", product photos with an empty alt inside links that name the product
    "/": shop_page("home.html", "Untitled document"),  # the title was never set
    "/rentals/": shop_page("rentals.html", "Bike rentals in Porto | Velo Ribeira"),
    "/rentals/touring": shop_page("rentals-touring.html", "Touring bike rental | Velo Ribeira"),
    "/tours": Redirect("/tours/"),  # decoy: trailing-slash redirect to the right page
    "/tours/": shop_page("tours.html", "Guided bike tours from Porto | Velo Ribeira"),
    "/tours/coastal-ride-vila-do-conde": shop_page("tour-coastal.html", "Coastal ride to Vila do Conde | Velo Ribeira"),
    "/tours/calendar": shop_page("tour-calendar.html", "Tour calendar | Velo Ribeira"),  # still the 2023 season
    "/tours/geres-weekend": Page("veloribeira/server-error.html", status=500),
    "/tours/aveiro-lagoon": Redirect("/tours/", 302),  # dropped tour, sent to the tour list
    "/shop/": shop_page("shop.html", "Shop bikes and gear | Velo Ribeira"),
    "/products/cargo-duo": shop_page("product-unavailable.html", "Cargo Duo e-bike | Velo Ribeira"),  # soft 404
    "/blog/": shop_page("blog.html", "Blog | Velo Ribeira"),
    "/blog/douro-valley-3-day-itinerary": shop_page("post-douro-valley.html", "Riding the Douro Valley: a 3-day itinerary | Velo Ribeira"),
    "/blog/bike-stolen-what-to-do": shop_page("post-bike-stolen.html", "Bike stolen in Porto? What to do next | Velo Ribeira"),  # decoy
    "/blog/chain-care-basics": shop_page("post-chain-care.html", "Chain care basics: clean, lube, repeat | Velo Ribeira"),
    "/blog/where-to-stay-pinhao": Redirect("/blog/"),  # deleted post, sent to the blog index
    "/news/spring-service-special": Redirect("/"),  # old news post, sent to the homepage
    "/events": Page("veloribeira/events.html"),  # 200 with a meta refresh to the homepage
    "/promo/end-of-season": Redirect("/promo/end-of-season/"),  # these two redirect to each other
    "/promo/end-of-season/": Redirect("/promo/end-of-season"),
    "/booking": shop_page("booking.html", "Book online | Velo Ribeira", delay=25),  # hangs
    "/gift-cards": Page("veloribeira/server-error.html", status=500, head_status=200),  # only HEAD looks fine
    "/account/orders": Redirect("/login?next=%2Faccount%2Forders", 302),  # decoy: needs an account
    "/downloads/size-guide.pdf": Redirect("/login?next=%2Fdownloads%2Fsize-guide.pdf", 302),  # public file behind login
    "/downloads/rental-terms.pdf": File(simple_pdf([
        "Velo Ribeira - Rental terms and conditions",
        "",
        "1. Rentals start when the bike leaves the shop and end when it is returned.",
        "2. A deposit of 100 EUR is held on a card and released on return.",
        "3. The renter is responsible for the bike and must lock it whenever it is left.",
        "4. Late returns are charged at the hourly rate, up to the daily rate.",
    ]), "application/pdf"),
    "/login": shop_page("login.html", "Sign in | Velo Ribeira"),
    "/cart": shop_page("cart.html", "Your basket | Velo Ribeira"),
    "/help/shipping-and-delivery": shop_page("help-shipping.html", "Shipping and delivery | Velo Ribeira"),
    "/help/returns-and-refunds": shop_page("help-returns.html", "Returns and refunds | Velo Ribeira"),
    "/help/warranty": shop_page("help-warranty.html", "Warranty and repairs | Velo Ribeira"),
    "/about": shop_page("about.html", "About us | Velo Ribeira"),
    "/partners": shop_page("partners.html", "Partners and friends | Velo Ribeira"),
    "/careers": shop_page("careers.html", "Careers | Velo Ribeira"),
    "/contact": shop_page("contact.html", "Contact us | Velo Ribeira"),
    "/privacy": shop_page("privacy.html", "Privacy policy | Velo Ribeira"),
    "/robots.txt": File(b"User-agent: *\nDisallow:\n", "text/plain"),
}
shop_routes |= {
    f"/products/{slug}": shop_page("product.html", f"{product['name']} | Velo Ribeira",
                                   context={"slug": slug, **IN_STOCK, **product})
    for slug, product in PRODUCTS.items()
}
shop_routes |= {f"/static/img/{name}": File(solid_png(*size), "image/png") for name, size in IMAGES.items()}

SITES = {
    MAIN_HOST: Site(
        routes=shop_routes,
        # any other product URL gets a 200 "couldn't find the product" page: a soft 404
        catch_all={"/products/": shop_page("product-missing.html", "Shop | Velo Ribeira")},
        not_found=shop_page("not-found.html", "Page not found | Velo Ribeira", status=404),
        static=True,
    ),
    "www.douroride.pt": Site(
        routes={
            "/": douro_page("home.html", "Douro Ride | Passeios de bicicleta no Douro"),
            "/passeios/": douro_page("passeios.html", "Passeios | Douro Ride"),
        } | {
            f"/passeios/{slug}": douro_page("passeio.html", f"{passeio['name']} | Douro Ride", context=passeio)
            for slug, passeio in PASSEIOS.items()
        },
        # any other tour URL gets a 200 "tour not available" page: a soft 404
        catch_all={"/passeios/": douro_page("passeio-indisponivel.html", "Douro Ride | Passeios de bicicleta no Douro")},
        not_found=douro_page("not-found.html", "Página não encontrada | Douro Ride", status=404),
        latency=(0.1, 0.3),
    ),
    "www.trailgear-supply.com": Site(
        routes={
            "/": trailgear_page("home.html", "Trailgear Supply | Touring and gravel components"),
            "/support/warranty-registration": Redirect("/"),  # page removed, sent to the homepage
        },
        not_found=trailgear_page("not-found.html", "404 | Trailgear Supply", status=404),
        latency=(0.08, 0.2),
    ),
    "routes.pedalmap.eu": Site(
        routes={
            "/": pedalmap_page("home.html", "PedalMap | Plan, share and ride"),
        } | {
            f"/r/{route_id}": pedalmap_page("route.html", f"{route['name']} | PedalMap", context={"id": route_id, **route})
            for route_id, route in PEDALMAP_ROUTES.items()
        },
        # any other route gets a 200 "route isn't available" page: a soft 404
        catch_all={"/r/": pedalmap_page("route-missing.html", "PedalMap | Plan, share and ride")},
        not_found=pedalmap_page("not-found.html", "Page not found | PedalMap", status=404),
        latency=(0.05, 0.12),
    ),
    "www.portobikefest.pt": Site(  # expired domain, now a parking page on every path
        routes={},
        catch_all={"/": Page("portobikefest/parked.html")},
        not_found=Page("portobikefest/parked.html"),
        latency=(0.2, 0.45),
    ),
}


def find(site, path):
    if path in site.routes:
        return site.routes[path]
    if site.static and path.startswith("/static/"):
        file = (STATIC / path.removeprefix("/static/")).resolve()
        if file.is_file() and file.is_relative_to(STATIC):
            return File(file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")
    for prefix, page in site.catch_all.items():
        if path.startswith(prefix):
            return page
    return site.not_found


def render(page):
    html = (TEMPLATES / page.template).read_text(encoding="utf-8")
    for key, value in page.context.items():
        html = html.replace("{{" + key + "}}", value)
    if page.layout:
        layout = (TEMPLATES / page.layout).read_text(encoding="utf-8")
        html = layout.replace("{{title}}", page.title).replace("{{content}}", html)
    return html


class Handler(BaseHTTPRequestHandler):
    def version_string(self):
        return "nginx"  # the Server header a small shop's site would send

    def do_GET(self):
        self.respond(send_body=True)

    def do_HEAD(self):
        self.respond(send_body=False)

    def respond(self, send_body):
        host = self.headers.get("Host", "").split(":")[0].lower()
        site = SITES.get(host, SITES[MAIN_HOST])
        response = find(site, urlsplit(self.path).path)
        if self.server.latency:
            time.sleep(random.uniform(*site.latency))
        try:
            if isinstance(response, Redirect):
                reason = f"{response.status} {HTTPStatus(response.status).phrase}"
                body = (f"<html><head><title>{reason}</title></head><body><center><h1>{reason}</h1></center>"
                        "<hr><center>nginx</center></body></html>")
                self.send(response.status, body.encode(), "text/html", send_body, location=self.localize(response.location))
            elif isinstance(response, File):
                self.send(200, response.body, response.content_type, send_body)
            else:
                time.sleep(response.delay)
                status = response.head_status if not send_body and response.head_status else response.status
                self.send(status, self.localize(render(response)).encode(), "text/html; charset=utf-8", send_body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client stopped waiting, e.g. on /booking

    def send(self, status, body, content_type, send_body, location=None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def localize(self, text):
        # on a port other than 80, links between the sites need the port too
        port = self.server.server_port
        if port == 80:
            return text
        for host in [*SITES, UNREACHABLE_HOST]:
            text = text.replace(f"http://{host}/", f"http://{host}:{port}/")
        return text

    def log_message(self, format, *args):
        headers = getattr(self, "headers", None)
        host = headers.get("Host", "-") if headers is not None else "-"
        print(f"{host} {format % args}")


def check_hosts():
    expected = {host: "127.0.0.1" for host in SITES} | {UNREACHABLE_HOST: "127.0.0.2"}
    for host, address in expected.items():
        try:
            resolved = socket.gethostbyname(host)
        except OSError:
            resolved = None
        if resolved != address:
            print(f"Warning: {host} resolves to {resolved}, not {address}. See the setup notes at the top of server.py.")


def main():
    parser = argparse.ArgumentParser(description="Serve the Velo Ribeira test site and the sites it links to.")
    parser.add_argument("--port", type=int, default=80, help="port to listen on (default 80, so URLs carry no port)")
    parser.add_argument("--no-latency", action="store_true", help="answer straight away instead of adding a realistic delay")
    args = parser.parse_args()

    check_hosts()
    try:
        # only on 127.0.0.1, so that www.ciclovia-norte.pt (127.0.0.2) refuses connections
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except PermissionError:
        raise SystemExit(f"Not allowed to listen on port {args.port}. Either run\n"
                         "    sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80\n"
                         "or pass --port 8080.")
    server.latency = not args.no_latency
    port = "" if args.port == 80 else f":{args.port}"
    print(f"Serving http://{MAIN_HOST}{port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
