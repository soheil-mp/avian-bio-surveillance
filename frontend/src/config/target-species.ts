export type TargetSpecies = {
  english: string;
  scientific: string;
  sovonId: number;
  birdweatherId: number;
};

export const TARGET_SPECIES_LIST: TargetSpecies[] = [
  { english: "Eurasian Blackbird", scientific: "Turdus merula", sovonId: 11870, birdweatherId: 35 },
  { english: "Song Thrush", scientific: "Turdus philomelos", sovonId: 12000, birdweatherId: 61 },
  { english: "Dunnock", scientific: "Prunella modularis", sovonId: 10840, birdweatherId: 44 },
  { english: "Eurasian Wren", scientific: "Troglodytes troglodytes", sovonId: 10660, birdweatherId: 5 },
  { english: "European Robin", scientific: "Erithacus rubecula", sovonId: 10990, birdweatherId: 6 },
  { english: "Eurasian Blackcap", scientific: "Sylvia atricapilla", sovonId: 12770, birdweatherId: 62 },
  { english: "Willow Warbler", scientific: "Phylloscopus trochilus", sovonId: 13120, birdweatherId: 81 },
  { english: "Common Chaffinch", scientific: "Fringilla coelebs", sovonId: 16360, birdweatherId: 25 },
  { english: "Western Jackdaw", scientific: "Coloeus monedula", sovonId: 15600, birdweatherId: 76 },
  { english: "Common Wood-Pigeon", scientific: "Columba palumbus", sovonId: 6700, birdweatherId: 407 },
  { english: "Eurasian Blue Tit", scientific: "Cyanistes caeruleus", sovonId: 14620, birdweatherId: 66 },
];

export const TARGET_SPECIES_MAP: Record<string, string> = Object.fromEntries(
  TARGET_SPECIES_LIST.map((species) => [species.english, species.scientific]),
);

export const TARGET_SPECIES = new Set(TARGET_SPECIES_LIST.map((species) => species.english));

export const TARGET_SPECIES_BY_BIRDWEATHER_ID = new Map(
  TARGET_SPECIES_LIST.map((species) => [String(species.birdweatherId), species]),
);