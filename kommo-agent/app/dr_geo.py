"""Deterministic Dominican Republic place -> province lookup.

The model reliably EXTRACTS the town the customer says; this table maps that
town / sector / municipality to its PROVINCE accurately, so the "Provincia:"
contact tag and the RD$45,000/RD$50,000 price tier do not depend on the model's
geography guess. Unknown places fall back to the model's own province (the first
field of the [[SECTOR:Provincia|Pueblo]] marker).

Province names match the study-coverage KB (16 provinces at RD$45,000, 15 at
RD$50,000). "Santo Domingo" = the province ring (Este/Norte/Oeste/Boca Chica);
"Distrito Nacional" = the capital core.
"""
import unicodedata

# province -> municipalities, notable municipal districts, and well-known sectors
_PROVINCES = {
    "Distrito Nacional": ["Distrito Nacional", "Capital", "Santo Domingo de Guzman"],
    "Santo Domingo": ["Santo Domingo", "Santo Domingo Este", "Santo Domingo Norte",
        "Santo Domingo Oeste", "Boca Chica", "Los Alcarrizos", "Pedro Brand",
        "San Antonio de Guerra", "Guerra", "La Caleta", "Villa Mella", "Sabana Perdida",
        "San Luis", "Bayona", "Herrera", "Los Mina", "Los Tres Ojos", "Invivienda",
        "Andres", "La Victoria", "Hainamosa", "Brisas del Este"],
    "Azua": ["Azua", "Padre Las Casas", "Peralta", "Sabana Yegua", "Estebania",
        "Guayabal", "Tabara Arriba", "Las Charcas", "Las Yayas de Viajama", "Pueblo Viejo"],
    "Bahoruco": ["Neiba", "Galvan", "Los Rios", "Tamayo", "Villa Jaragua"],
    "Barahona": ["Barahona", "Cabral", "Enriquillo", "Paraiso", "Vicente Noble",
        "El Penon", "La Cienaga", "Fundacion", "Las Salinas", "Polo", "Jaquimeyes"],
    "Dajabon": ["Dajabon", "Loma de Cabrera", "Partido", "Restauracion", "El Pino"],
    "Duarte": ["San Francisco de Macoris", "Arenoso", "Castillo", "Pimentel",
        "Villa Riva", "Las Guaranas", "Eugenio Maria de Hostos"],
    "Elias Pina": ["Comendador", "Elias Pina", "Banica", "El Llano", "Hondo Valle",
        "Pedro Santana", "Juan Santiago"],
    "El Seibo": ["El Seibo", "Miches", "Santa Cruz del Seibo"],
    "Espaillat": ["Moca", "Cayetano Germosen", "Gaspar Hernandez", "Jamao al Norte"],
    "Hato Mayor": ["Hato Mayor", "Sabana de la Mar", "El Valle"],
    "Hermanas Mirabal": ["Salcedo", "Tenares", "Villa Tapia"],
    "Independencia": ["Jimani", "Duverge", "La Descubierta", "Mella", "Postrer Rio", "Cristobal"],
    "La Altagracia": ["Higuey", "Salvaleon de Higuey", "San Rafael del Yuma",
        "Punta Cana", "Bavaro", "Veron", "La Otra Banda", "Boca de Yuma"],
    "La Romana": ["La Romana", "Guaymate", "Villa Hermosa"],
    "La Vega": ["La Vega", "Concepcion de La Vega", "Constanza", "Jarabacoa",
        "Jima Abajo", "Rincon", "Rio Verde Arriba"],
    "Maria Trinidad Sanchez": ["Nagua", "Cabrera", "El Factor", "Rio San Juan"],
    "Monsenor Nouel": ["Bonao", "Maimon", "Piedra Blanca"],
    "Monte Cristi": ["Monte Cristi", "San Fernando de Monte Cristi", "Castanuelas",
        "Guayubin", "Las Matas de Santa Cruz", "Pepillo Salcedo", "Villa Vasquez"],
    "Monte Plata": ["Monte Plata", "Bayaguana", "Peralvillo", "Sabana Grande de Boya", "Yamasa"],
    "Pedernales": ["Pedernales", "Oviedo"],
    "Peravia": ["Bani", "Nizao", "Matanzas", "Villa Fundacion"],
    "Puerto Plata": ["Puerto Plata", "San Felipe de Puerto Plata", "Altamira",
        "Guananico", "Imbert", "Los Hidalgos", "Luperon", "Sosua", "Villa Isabela",
        "Villa Montellano", "Cabarete", "Montellano", "Maimon PP"],
    "Samana": ["Samana", "Santa Barbara de Samana", "Las Terrenas", "Sanchez"],
    "Sanchez Ramirez": ["Cotui", "Cevicos", "Fantino", "La Mata"],
    "San Cristobal": ["San Cristobal", "Bajos de Haina", "Haina", "Cambita Garabitos",
        "Los Cacaos", "Sabana Grande de Palenque", "Palenque", "San Gregorio de Nigua",
        "Nigua", "Villa Altagracia", "Yaguate"],
    "San Jose de Ocoa": ["San Jose de Ocoa", "Ocoa", "Rancho Arriba", "Sabana Larga"],
    "San Juan": ["San Juan", "San Juan de la Maguana", "Bohechio", "El Cercado",
        "Juan de Herrera", "Las Matas de Farfan", "Vallejuelo"],
    "San Pedro de Macoris": ["San Pedro de Macoris", "San Pedro", "Consuelo",
        "Guayacanes", "Quisqueya", "Ramon Santana", "Los Llanos", "Juan Dolio"],
    "Santiago": ["Santiago", "Santiago de los Caballeros", "Bisono", "Villa Bisono",
        "Navarrete", "Janico", "Licey al Medio", "Punal", "Sabana Iglesia",
        "San Jose de las Matas", "Tamboril", "Villa Gonzalez", "Baitoa", "Pedro Garcia",
        "La Herradura", "Gurabo", "Cienfuegos"],
    "Santiago Rodriguez": ["Santiago Rodriguez", "Sabaneta", "San Ignacio de Sabaneta",
        "Los Almacigos", "Moncion"],
    "Valverde": ["Mao", "Santa Cruz de Mao", "Esperanza", "Laguna Salada"],
}

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower().strip().strip(".,;!?")
    for pre in ("en ", "el ", "la ", "los ", "las ", "provincia de ", "provincia ",
                "municipio de ", "municipio ", "sector ", "pueblo de ", "pueblo "):
        if s.startswith(pre):
            s = s[len(pre):]
    return " ".join(s.split())

# normalized place -> province
_LOOKUP = {}
for prov, places in _PROVINCES.items():
    _LOOKUP[_norm(prov)] = prov
    for p in places:
        _LOOKUP.setdefault(_norm(p), prov)

# aliases that are not obvious from the names above
for alias, prov in {
    "sabaneta": "Santiago Rodriguez", "mao": "Valverde",
    "ocoa": "San Jose de Ocoa", "sfm": "Duarte",
}.items():
    _LOOKUP.setdefault(_norm(alias), prov)


def province_for(town: str):
    """Return the province for a town/sector/municipality, or None if unknown."""
    if not town:
        return None
    return _LOOKUP.get(_norm(town))
