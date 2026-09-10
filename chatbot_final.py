"""
CHATBOT SERVEUR - Version Flask pour Ella Queen
==============================================
"""

import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic

app = Flask(__name__)
CORS(app)  # Permet à votre site sur GitHub de communiquer avec ce serveur

# Récupération sécurisée de la clé API depuis les variables d'environnement de Render
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=API_KEY)

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

        # Filtrage intelligent du catalogue en fonction de la question
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

        reponse_texte = reponse.content[0].text
        return jsonify({"reponse": reponse_texte})

    except Exception as e:
        print(f"Erreur : {e}")
        return jsonify({"reponse": "Désolé, une erreur technique est survenue."}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)