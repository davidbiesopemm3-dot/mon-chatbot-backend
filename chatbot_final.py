"""
CHATBOT SERVEUR - Version Flask pour Ella Queen
==============================================
"""

import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic

app = Flask(__name__)
CORS(app)  # Permet à votre site sur GitHub de communiquer avec ce serveur

# Récupération sécurisée de la clé API depuis les variables d'environnement de Render
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=API_KEY)

# Variables pour la connexion WhatsApp
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

# ============================================
# CATALOGUE DE LA BOUTIQUE ELLA (200 articles)
# ============================================
types_articles_ella = [
    "Robe", "Chemise", "Pantalon", "Jupe", "Blazer", "T-shirt", "Short", "Combinaison",
    "Veste", "Manteau", "Cardigan", "Pull", "Crop top", "Tuniques", "Kaftan",
    "Boubou", "Kimono", "Ensemble", "Jean", "Legging", "Jogging", "Sweat",
    "Top", "Caraco", "Bustier", "Chemisier", "Trench", "Gilet", "Blouse",
    "Polo", "Salopette", "Tutu", "Pareo", "Poncho", "Boléro", "Capeline",
    "Surchemise", "Pyjama", "Nuisette", "Peignoir"
]

catalogue_ella = []
id_c = 1
for t in types_articles_ella:
    for i in range(1, 6):
        prix = 5000 + (id_c * 400) % 20500
        catalogue_ella.append({
            "type": t.lower(),
            "nom": f"{t} Modèle {chr(64+i)}",
            "prix": f"{prix:,}".replace(",", " ") + " FCFA"
        })
        id_c += 1

INFOS_BOUTIQUE_ELLA = """
Nom de la boutique : Chez Ella (Prêt-à-porter féminin)
Horaires : du lundi au samedi, de 9h à 19h
Livraison : oui, partout à Abidjan, sous 48h
Paiement accepté : Orange Money, Wave, Cash
Adresse : Marcory, Abidjan
Catalogue riche de 40 types de vêtements différents (robes, chemises, pantalons, boubous, vestes, pyjamas, etc.).
"""


def rechercher_produits_pertinents(message_client, catalogue):
    if not catalogue:
        return ""
    mots = message_client.lower().split()
    articles_trouves = [
        art for art in catalogue
        if any(mot in art["type"] or mot in art["nom"].lower() for mot in mots)
    ]
    if not articles_trouves:
        articles_trouves = catalogue[:15]
       
    texte_catalogue = "Exemples d'articles disponibles correspondants :\n"
    for art in articles_trouves[:20]:
        texte_catalogue += f"- {art['nom']} ({art['type']}) : {art['prix']}\n"
    return texte_catalogue


def generer_reponse_claude(user_message):
    """Fonction commune : envoie le message à Claude et renvoie le texte de la réponse."""
    contexte_catalogue = rechercher_produits_pertinents(user_message, catalogue_ella)

    instructions_systeme = f"""
Tu es l'assistant de la boutique.
Réponds aux clients de façon courte, chaleureuse et professionnelle,
en te basant UNIQUEMENT sur les informations suivantes :

{INFOS_BOUTIQUE_ELLA}

{contexte_catalogue}

Si tu ne connais pas la réponse, dis poliment qu'un membre de l'équipe va recontacter le client.
Ne mentionne jamais que tu es une intelligence artificielle.
"""

    reponse = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=instructions_systeme,
        messages=[{"role": "user", "content": user_message}]
    )

    return reponse.content[0].text


def envoyer_message_whatsapp(numero_destinataire, texte):
    """Envoie un message texte via l'API WhatsApp Cloud."""
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destinataire,
        "type": "text",
        "text": {"body": texte}
    }
    reponse = requests.post(url, headers=headers, json=payload)
    print(f"Réponse envoi WhatsApp : {reponse.status_code} - {reponse.text}")
    return reponse


@app.route("/")
def home():
    return "Le serveur du chatbot Ella Queen est en ligne !"


@app.route("/message", methods=["POST"])
def chat():
    try:
        data = request.get_json()
        user_message = data.get("message", "")
       
        if not user_message:
            return jsonify({"reponse": "Veuillez entrer un message."}), 400

        reponse_texte = generer_reponse_claude(user_message)
        return jsonify({"reponse": reponse_texte})

    except Exception as e:
        print(f"Erreur : {e}")
        return jsonify({"reponse": "Désolé, une erreur technique est survenue."}), 500


@app.route("/webhook", methods=["GET"])
def verifier_webhook():
    """Meta appelle cette route une seule fois, pour vérifier que le webhook t'appartient."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook vérifié avec succès !")
        return challenge, 200
    else:
        print("Échec de la vérification du webhook.")
        return "Erreur de vérification", 403


@app.route("/webhook", methods=["POST"])
def recevoir_message_whatsapp():
    """Meta appelle cette route à chaque nouveau message reçu sur WhatsApp."""
    try:
        data = request.get_json()
        print(f"Webhook reçu : {data}")

        entry = data.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages")

        if messages:
            message = messages[0]
            numero_expediteur = message.get("from")
            texte_recu = message.get("text", {}).get("body", "")

            if texte_recu:
                reponse_texte = generer_reponse_claude(texte_recu)
                envoyer_message_whatsapp(numero_expediteur, reponse_texte)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Erreur webhook : {e}")
        return jsonify({"status": "error"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)