"""Check words of the pairing — 256 short French words, ASCII only (typed
or read aloud on two machines, no accent to mistype). Four words carry 32
bits: one guess per pairing attempt, approved by a human who compares
them — the short-authentication-string idea of Bluetooth numeric
comparison. Order is part of the contract: never reorder or edit, only a
new list under a new derivation label."""

WORDS = (
    "ancre", "arbre", "avion", "balle", "banc", "barque", "bateau", "berger",
    "bijou", "biscuit", "bougie", "boussole", "brique", "bureau", "cabane",
    "cactus", "cadre", "camion", "canard", "canne", "carte", "castor",
    "cerise", "chaise", "chameau", "champ", "chapeau", "chat", "chemin",
    "cheval", "chien", "citron", "clown", "cobra", "coffre", "colline",
    "corde", "coton", "coude", "crabe", "crayon", "cygne", "dauphin", "domino",
    "dragon", "drapeau", "fanfare", "farine", "fenouil", "fermier", "feuille",
    "ficelle", "figue", "flamme", "fleuve", "fontaine", "forge", "fourmi",
    "fraise", "frigo", "fromage", "galet", "gant", "girafe", "glacier",
    "gomme", "gorille", "goutte", "grenier", "griffe", "grotte", "guitare",
    "hamac", "hamster", "harpe", "hibou", "homard", "horloge", "iceberg",
    "igloo", "jardin", "jasmin", "jongleur", "journal", "jungle", "kayak",
    "koala", "lampe", "lapin", "lavande", "levier", "licorne", "lierre",
    "limace", "lion", "loutre", "lune", "lutin", "manteau", "marmotte",
    "marteau", "masque", "melon", "menthe", "miroir", "montagne", "moto",
    "moulin", "mouton", "muguet", "musique", "navire", "neige", "nuage",
    "olive", "ombre", "orage", "orange", "ortie", "otarie", "ours", "outil",
    "panda", "panier", "papier", "parapluie", "passoire", "patin", "pelle",
    "perle", "phare", "piano", "pigeon", "pinceau", "pirate", "plage",
    "planche", "plume", "poisson", "pomme", "pont", "potiron", "poulpe",
    "prairie", "puzzle", "radeau", "raisin", "rameau", "raquette", "renard",
    "requin", "rivage", "robot", "rocher", "roseau", "rubis", "ruche", "sable",
    "sabot", "safran", "salade", "sapin", "sardine", "satin", "sauge", "savon",
    "serpent", "sirop", "soleil", "souris", "sucre", "tablier", "tambour",
    "tapis", "taupe", "tigre", "tilleul", "tipi", "tomate", "tonneau",
    "torche", "tortue", "toucan", "toupie", "tracteur", "train", "trompette",
    "tulipe", "tunnel", "vache", "valise", "vallon", "vanille", "verger",
    "violon", "voilier", "volcan", "wagon", "yaourt", "zeste", "abricot",
    "agneau", "aigle", "alouette", "amande", "anneau", "arrosoir", "artichaut",
    "autruche", "baleine", "bambou", "banane", "baobab", "basilic",
    "betterave", "biche", "blaireau", "bocal", "bonnet", "bouleau", "bourdon",
    "boxeur", "brebis", "brioche", "brocoli", "brouette", "buffle", "cacao",
    "caillou", "carotte", "cascade", "casque", "cerf", "chalet", "chardon",
    "chouette", "cigale", "citrouille", "cocotte", "colombe", "concombre",
    "coquille", "corbeau", "cornichon", "courgette", "crocodile", "dinde",
    "escargot", "faucon", "flamant", "framboise", "furet", "gazelle",
    "grenouille", "mouette", "pingouin",
)
