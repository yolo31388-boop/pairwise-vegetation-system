"""
pairwise-vegetation-system - VegetationSystem

A self-contained vegetation system:

- render_grass: interactive bending, wind sway and distance-based LOD
- check_grass_collision: volume-based collision with smooth bend/recovery
- distribute_vegetation: terrain-aware, density-controlled, deterministic
- animate_vegetation: per-blade phase, wind direction, height-based amplitude
- cull_vegetation: frustum + occlusion + distance culling with draw-call batching
"""

import json
import math
import os
import random


def _length2(x, z):
    return math.sqrt(x * x + z * z)


def _normalize2(x, z):
    length = _length2(x, z)
    if length == 0.0:
        return (0.0, 0.0)
    return (x / length, z / length)


class VegetationSystem:
    # LOD distance thresholds (near -> far).
    LOD_DISTANCES = (20.0, 60.0, 150.0)
    # Fraction of blades rendered per LOD level (LOD0 = full detail).
    LOD_DENSITY = (1.0, 0.5, 0.25, 0.1)
    # Collision tuning.
    DEFAULT_CHARACTER_RADIUS = 0.5
    BEND_RESPONSE_SPEED = 8.0    # how fast grass bends toward its target
    RECOVERY_SPEED = 2.0         # how fast grass springs back upright
    # Distribution tuning.
    MAX_TREE_SLOPE_DEG = 30.0
    MAX_GRASS_SLOPE_DEG = 45.0
    WATER_LEVEL = 0.0
    MIN_SPACING = {"tree": 4.0, "bush": 2.0, "grass": 0.4}
    # Culling tuning.
    DEFAULT_CULL_DISTANCE = 200.0
    BATCH_CELL_SIZE = 10.0

    def __init__(self, seed=1337):
        self.state = {}
        self.seed = seed
        # blade id -> current bend amount in [0, 1]; persists across frames so
        # grass bends smoothly and recovers after the character leaves.
        self.grass_states = {}
        # cache key -> placement list; keeps distribution stable across calls.
        self.distribution_cache = {}

    # ------------------------------------------------------------------
    # Grass rendering: interaction bending + wind + LOD
    # ------------------------------------------------------------------
    def render_grass(self, grass_blades=None, characters=None, wind=None,
                     camera=None, time=0.0, dt=1.0 / 60.0):
        if grass_blades is None:
            grass_blades = self._default_grass_field()
        if characters is None:
            characters = []
        if wind is None:
            wind = {"direction": (1.0, 0.0), "strength": 0.5}
        if camera is None:
            camera = {"position": (0.0, 0.0, 0.0)}

        # 1. Interactive bending: characters push grass aside (volume-based,
        #    smoothed and recovering over time).
        bend_map = {}
        for character in characters:
            bend_map.update(self.check_grass_collision(
                character=character, grass_blades=grass_blades, dt=dt))
        if not characters:
            # No one around: let everything recover.
            self.check_grass_collision(character=None, grass_blades=grass_blades, dt=dt)

        # 2. Wind sway per blade (phase offset so the field ripples).
        wind_dir = _normalize2(*wind.get("direction", (1.0, 0.0)))
        wind_strength = float(wind.get("strength", 0.5))

        # 3. LOD: pick detail level per blade from camera distance.
        cam_x, cam_y, cam_z = camera.get("position", (0.0, 0.0, 0.0))
        rendered = []
        lod_counts = [0, 0, 0, 0]
        for blade in grass_blades:
            blade_id = blade.get("id", id(blade))
            bx, _, bz = blade.get("position", (0.0, 0.0, 0.0))
            distance = math.sqrt((bx - cam_x) ** 2 + (bz - cam_z) ** 2)
            lod = self._lod_for_distance(distance)
            # Sparse sampling at higher LODs: deterministic per blade id.
            keep = self.LOD_DENSITY[lod]
            if keep < 1.0 and (hash(blade_id) & 0xFFFF) / 0xFFFF > keep:
                continue
            lod_counts[lod] += 1
            phase = self._blade_phase(blade_id)
            sway = wind_strength * math.sin(time * 2.0 + phase)
            rendered.append({
                "id": blade_id,
                "position": blade.get("position", (0.0, 0.0, 0.0)),
                "lod": lod,
                "bend": bend_map.get(blade_id, self.grass_states.get(blade_id, 0.0)),
                "wind_sway": sway,
                "wind_direction": wind_dir,
            })

        return {
            "blades": rendered,
            "lod_counts": lod_counts,
            "wind": {"direction": wind_dir, "strength": wind_strength},
        }

    # ------------------------------------------------------------------
    # Grass collision: character volume, smooth transition, recovery
    # ------------------------------------------------------------------
    def check_grass_collision(self, character=None, grass_blades=None, dt=1.0 / 60.0):
        if grass_blades is None:
            grass_blades = self._default_grass_field()
        dt = max(float(dt), 1e-6)

        targets = {}
        if character is not None:
            # Volume check: every blade inside the character radius bends,
            # weighted by how deep inside the volume it is (edge blades bend
            # less than blades at the centre).
            cx, _, cz = character.get("position", (0.0, 0.0, 0.0))
            radius = float(character.get("radius", self.DEFAULT_CHARACTER_RADIUS))
            for blade in grass_blades:
                blade_id = blade.get("id", id(blade))
                bx, _, bz = blade.get("position", (0.0, 0.0, 0.0))
                distance = _length2(bx - cx, bz - cz)
                if distance <= radius:
                    # 1.0 at the centre, fading to 0.0 at the volume edge.
                    targets[blade_id] = 1.0 - (distance / radius)

        result = {}
        touched = set(targets) | {
            bid for bid in self.grass_states
            if any(b.get("id", id(b)) == bid for b in grass_blades)
        }
        for blade_id in touched:
            current = self.grass_states.get(blade_id, 0.0)
            target = targets.get(blade_id, 0.0)
            if target > current:
                # Smooth transition towards bent.
                rate = self.BEND_RESPONSE_SPEED
            else:
                # Recovery: spring back upright once the character leaves.
                rate = self.RECOVERY_SPEED
            new_bend = current + (target - current) * min(1.0, rate * dt)
            new_bend = max(0.0, min(1.0, new_bend))
            self.grass_states[blade_id] = new_bend
            result[blade_id] = new_bend
        return result

    # ------------------------------------------------------------------
    # Vegetation distribution: terrain-aware, density-limited, persistent
    # ------------------------------------------------------------------
    def distribute_vegetation(self, terrain=None, area=(0.0, 0.0, 100.0, 100.0),
                              density=None, seed=None, save_path=None):
        if terrain is None:
            terrain = self._default_terrain()
        if density is None:
            density = {"tree": 0.01, "bush": 0.05, "grass": 1.0}
        seed = self.seed if seed is None else seed

        cache_key = (seed, tuple(area), tuple(sorted(density.items())))
        if cache_key in self.distribution_cache:
            return list(self.distribution_cache[cache_key])
        if save_path and os.path.exists(save_path):
            # Persistent distribution: reload instead of regenerating.
            with open(save_path, "r", encoding="utf-8") as handle:
                placements = [tuple(p) for p in json.load(handle)]
            self.distribution_cache[cache_key] = placements
            return list(placements)

        rng = random.Random(seed)  # deterministic: same seed -> same layout
        min_x, min_z, max_x, max_z = area
        placements = []
        for veg_type, per_area in density.items():
            count = int(per_area * (max_x - min_x) * (max_z - min_z))
            spacing = self.MIN_SPACING.get(veg_type, 1.0)
            attempts = 0
            placed_of_type = 0
            while placed_of_type < count and attempts < count * 20:
                attempts += 1
                x = rng.uniform(min_x, max_x)
                z = rng.uniform(min_z, max_z)
                height = terrain.get("height", lambda _x, _z: 1.0)(x, z)
                slope = terrain.get("slope", lambda _x, _z: 0.0)(x, z)

                # Terrain rules: nothing underwater, trees only on gentle
                # slopes, grass tolerates steeper ground.
                if height < self.WATER_LEVEL:
                    continue
                max_slope = (self.MAX_TREE_SLOPE_DEG if veg_type == "tree"
                             else self.MAX_GRASS_SLOPE_DEG)
                if slope > max_slope:
                    continue
                # Density control: enforce minimum spacing between instances.
                if any(_length2(x - px, z - pz) < spacing
                       for _t, px, pz, _h in placements):
                    continue
                placements.append((veg_type, x, z, height))
                placed_of_type += 1

        self.distribution_cache[cache_key] = placements
        if save_path:
            with open(save_path, "w", encoding="utf-8") as handle:
                json.dump(placements, handle)
        return list(placements)

    # ------------------------------------------------------------------
    # Vegetation animation: phase offsets, wind direction, height detail
    # ------------------------------------------------------------------
    def animate_vegetation(self, vegetation=None, wind=None, time=0.0,
                           segments=4):
        if vegetation is None:
            vegetation = [
                {"id": index, "position": (float(index), 0.0, 0.0), "height": 1.0}
                for index in range(8)
            ]
        if wind is None:
            wind = {"direction": (1.0, 0.0), "strength": 0.5}
        wind_dir = _normalize2(*wind.get("direction", (1.0, 0.0)))
        wind_strength = float(wind.get("strength", 0.5))

        states = []
        for item in vegetation:
            item_id = item.get("id", id(item))
            # Per-blade phase: blades ripple instead of moving in lockstep.
            phase = self._blade_phase(item_id)
            base_angle = wind_strength * math.sin(time * 2.0 + phase)
            # Height detail: the tip sways much more than the root.
            segment_offsets = []
            for seg in range(1, segments + 1):
                height_factor = (seg / segments) ** 2
                segment_offsets.append({
                    "height": height_factor,
                    "angle": base_angle * height_factor,
                    "offset": (wind_dir[0] * base_angle * height_factor,
                               wind_dir[1] * base_angle * height_factor),
                })
            states.append({
                "id": item_id,
                "phase": phase,
                "wind_direction": wind_dir,
                "segments": segment_offsets,
            })
        return states

    # ------------------------------------------------------------------
    # Vegetation culling: frustum + occlusion + distance, batched draws
    # ------------------------------------------------------------------
    def cull_vegetation(self, vegetation=None, camera=None, occluders=None,
                        max_distance=DEFAULT_CULL_DISTANCE):
        if vegetation is None:
            vegetation = [
                {"id": index, "type": "grass",
                 "position": (float(index) * 5.0, 0.0, 10.0)}
                for index in range(8)
            ]
        if camera is None:
            camera = {"position": (0.0, 0.0, 0.0), "forward": (0.0, 0.0, 1.0),
                      "fov_deg": 90.0}
        if occluders is None:
            occluders = []

        cam_pos = camera.get("position", (0.0, 0.0, 0.0))
        forward = camera.get("forward", (0.0, 0.0, 1.0))
        fov_cos = math.cos(math.radians(camera.get("fov_deg", 90.0)) / 2.0)

        visible = []
        for item in vegetation:
            pos = item.get("position", (0.0, 0.0, 0.0))
            to_item = (pos[0] - cam_pos[0], pos[1] - cam_pos[1],
                       pos[2] - cam_pos[2])
            distance = math.sqrt(sum(c * c for c in to_item))
            # Distance culling: nothing renders beyond max_distance.
            if distance > max_distance or distance == 0.0:
                continue
            # Frustum culling.
            direction = tuple(c / distance for c in to_item)
            dot = sum(direction[i] * forward[i] for i in range(3))
            if dot < fov_cos:
                continue
            # Occlusion culling: hidden behind an occluder sphere.
            if any(self._is_occluded(cam_pos, pos, occluder)
                   for occluder in occluders):
                continue
            visible.append(item)

        # Batching: group survivors by (type, grid cell) into few draw calls
        # instead of one draw call per instance.
        batches = {}
        for item in visible:
            pos = item.get("position", (0.0, 0.0, 0.0))
            cell = (int(pos[0] // self.BATCH_CELL_SIZE),
                    int(pos[2] // self.BATCH_CELL_SIZE))
            key = (item.get("type", "grass"), cell)
            batches.setdefault(key, []).append(item.get("id", id(item)))

        return {
            "visible": visible,
            "culled": len(vegetation) - len(visible),
            "draw_calls": [
                {"type": key[0], "cell": key[1], "instances": members}
                for key, members in sorted(batches.items(), key=str)
            ],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _lod_for_distance(self, distance):
        for level, threshold in enumerate(self.LOD_DISTANCES):
            if distance < threshold:
                return level
        return len(self.LOD_DISTANCES)

    @staticmethod
    def _blade_phase(blade_id):
        # Stable pseudo-random phase in [0, 2*pi) per blade.
        return (hash(blade_id) % 6283) / 1000.0

    @staticmethod
    def _is_occluded(cam_pos, item_pos, occluder):
        """Sphere occluder fully between camera and item blocks the item."""
        ox, oy, oz = occluder.get("position", (0.0, 0.0, 0.0))
        radius = float(occluder.get("radius", 1.0))
        seg = (item_pos[0] - cam_pos[0], item_pos[1] - cam_pos[1],
               item_pos[2] - cam_pos[2])
        seg_len_sq = sum(c * c for c in seg)
        if seg_len_sq == 0.0:
            return False
        t = ((ox - cam_pos[0]) * seg[0] + (oy - cam_pos[1]) * seg[1]
             + (oz - cam_pos[2]) * seg[2]) / seg_len_sq
        if not 0.0 < t < 1.0:
            return False
        closest = (cam_pos[0] + seg[0] * t - ox,
                   cam_pos[1] + seg[1] * t - oy,
                   cam_pos[2] + seg[2] * t - oz)
        return sum(c * c for c in closest) < radius * radius

    @staticmethod
    def _default_grass_field():
        return [
            {"id": index,
             "position": ((index % 10) * 0.5, 0.0, (index // 10) * 0.5)}
            for index in range(100)
        ]

    @staticmethod
    def _default_terrain():
        return {
            "height": lambda x, z: 1.0 + 0.5 * math.sin(x * 0.05) * math.cos(z * 0.05),
            "slope": lambda x, z: 10.0,
        }
