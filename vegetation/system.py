"""
pairwise-vegetation-system - VegetationSystem

A self-contained vegetation system:

- render_grass: interactive bending, wind sway, LOD
- check_grass_collision: volume-based collision, smooth bend, recovery
- distribute_vegetation: terrain-aware, density-controlled, persisted
- animate_vegetation: per-blade phase, wind direction, height gradient
- cull_vegetation: frustum + occlusion + distance culling, batched draws
"""

import json
import math
import os
import random


def _vec2_normalize(v):
    length = math.hypot(v[0], v[1])
    if length == 0.0:
        return (1.0, 0.0)
    return (v[0] / length, v[1] / length)


class VegetationSystem:
    # LOD: (max_distance, blade_stride) - stride 1 = full detail
    LOD_LEVELS = ((20.0, 1), (60.0, 2), (150.0, 4))

    # Collision smoothing: bend in/out rates (per second)
    BEND_IN_RATE = 6.0
    RECOVER_RATE = 2.0

    # Distribution constraints
    MAX_SLOPE_DEG = 35.0
    WATER_LEVEL = 0.0
    TREE_LINE = 80.0
    MIN_SPACING = 2.0
    MAX_PER_CELL = 4
    CELL_SIZE = 10.0

    # Culling
    GRASS_CULL_DISTANCE = 200.0
    TREE_CULL_DISTANCE = 2000.0

    def __init__(self, seed=1337, cache_path="vegetation_distribution.json"):
        self.state = {}
        self.seed = seed
        self.time = 0.0
        self.wind = {"direction": (1.0, 0.0), "strength": 1.0, "speed": 2.0}
        self.cache_path = cache_path
        self._bend_state = {}  # blade id -> current bend amount 0..1
        self._distribution = None

    # ------------------------------------------------------------------
    # Rendering: interactive bending + wind + LOD
    # ------------------------------------------------------------------
    def render_grass(self, grass=None, characters=None, wind=None,
                     camera=None, dt=1.0 / 60.0):
        grass = grass if grass is not None else self.state.get("grass", [])
        characters = characters or []
        wind = wind or self.wind
        camera = camera or {"position": (0.0, 0.0, 0.0)}
        self.time += dt

        wind_dir = _vec2_normalize(wind.get("direction", (1.0, 0.0)))
        wind_strength = wind.get("strength", 1.0)
        wind_speed = wind.get("speed", 2.0)

        rendered = []
        lod_counts = {}
        for index, blade in enumerate(grass):
            pos = blade.get("position", (0.0, 0.0, 0.0))
            dist = math.dist(camera.get("position", (0.0, 0.0, 0.0)), pos)

            lod = self._lod_for_distance(dist)
            if lod is None:
                continue  # beyond all LOD ranges
            lod_index, stride = lod
            if stride > 1 and index % stride != 0:
                continue  # LOD thinning
            lod_counts[lod_index] = lod_counts.get(lod_index, 0) + 1

            # Wind sway: per-blade phase so blades don't move in lockstep
            phase = self._blade_phase(blade, index)
            sway = math.sin(self.time * wind_speed + phase) * wind_strength

            # Interactive bending from characters (volume-aware, smoothed)
            bend = self._interaction_bend(blade, characters, dt)

            rendered.append({
                "id": blade.get("id", index),
                "position": pos,
                "lod": lod_index,
                "wind_offset": (wind_dir[0] * sway, wind_dir[1] * sway),
                "bend": bend,
            })

        result = {
            "time": self.time,
            "rendered": rendered,
            "lod_counts": lod_counts,
            "wind": {"direction": wind_dir, "strength": wind_strength},
        }
        self.state["last_render"] = result
        return result

    def _lod_for_distance(self, dist):
        for i, (max_dist, stride) in enumerate(self.LOD_LEVELS):
            if dist <= max_dist:
                return (i, stride)
        return None

    def _blade_phase(self, blade, index):
        pos = blade.get("position", (0.0, 0.0, 0.0))
        return (pos[0] * 0.35 + pos[2] * 0.27 + index * 0.61) % (2.0 * math.pi)

    def _interaction_bend(self, blade, characters, dt):
        blade_id = blade.get("id", id(blade))
        pos = blade.get("position", (0.0, 0.0, 0.0))
        target = 0.0
        for char in characters:
            cpos = char.get("position", (0.0, 0.0, 0.0))
            radius = char.get("radius", 0.5)
            # Volume check: character capsule footprint vs blade position
            dx = pos[0] - cpos[0]
            dz = pos[2] - cpos[2]
            horizontal = math.hypot(dx, dz)
            height = char.get("height", 1.8)
            if horizontal <= radius and 0.0 <= pos[1] - cpos[1] <= height:
                target = max(target, 1.0 - horizontal / max(radius, 1e-6))
        current = self._bend_state.get(blade_id, 0.0)
        if target > current:
            current = min(target, current + self.BEND_IN_RATE * dt)
        else:
            current = max(target, current - self.RECOVER_RATE * dt)
        self._bend_state[blade_id] = current
        return current

    # ------------------------------------------------------------------
    # Collision: volume check + smooth transition + recovery
    # ------------------------------------------------------------------
    def check_grass_collision(self, character=None, grass=None, dt=1.0 / 60.0):
        character = character or {"position": (0.0, 0.0, 0.0), "radius": 0.5,
                                  "height": 1.8}
        grass = grass if grass is not None else self.state.get("grass", [])
        cpos = character.get("position", (0.0, 0.0, 0.0))
        radius = character.get("radius", 0.5)
        height = character.get("height", 1.8)

        collisions = []
        for index, blade in enumerate(grass):
            blade_id = blade.get("id", index)
            pos = blade.get("position", (0.0, 0.0, 0.0))
            dx = pos[0] - cpos[0]
            dz = pos[2] - cpos[2]
            horizontal = math.hypot(dx, dz)
            vertical_ok = 0.0 <= pos[1] - cpos[1] <= height
            inside = horizontal <= radius and vertical_ok

            target = 0.0
            if inside:
                # Bend scales with penetration depth into the volume
                target = 1.0 - horizontal / max(radius, 1e-6)

            current = self._bend_state.get(blade_id, 0.0)
            if target > current:
                current = min(target, current + self.BEND_IN_RATE * dt)
            else:
                # Recovery: grass springs back after the character leaves
                current = max(target, current - self.RECOVER_RATE * dt)
            self._bend_state[blade_id] = current

            if current > 0.0:
                collisions.append({
                    "id": blade_id,
                    "position": pos,
                    "bend": current,
                    "recovering": not inside,
                })

        result = {"character": cpos, "radius": radius, "height": height,
                  "collisions": collisions}
        self.state["last_collision"] = result
        return result

    # ------------------------------------------------------------------
    # Distribution: terrain-aware + density control + persistence
    # ------------------------------------------------------------------
    def distribute_vegetation(self, terrain=None, area=None, count=100,
                              kind="tree", cache_path=None, reload=False):
        cache_path = cache_path or self.cache_path

        # Persistence: reuse a saved distribution so positions are stable
        if not reload and self._distribution is not None:
            return self._distribution
        if not reload and os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as fh:
                self._distribution = json.load(fh)
            for inst in self._distribution.get("instances", []):
                if isinstance(inst.get("position"), list):
                    inst["position"] = tuple(inst["position"])
            return self._distribution

        terrain = terrain or {}
        area = area or {"min": (-50.0, -50.0), "max": (50.0, 50.0)}
        rng = random.Random(self.seed)  # deterministic, not wall-clock random

        placed = []
        cell_counts = {}
        attempts = 0
        max_attempts = count * 20
        while len(placed) < count and attempts < max_attempts:
            attempts += 1
            x = rng.uniform(area["min"][0], area["max"][0])
            z = rng.uniform(area["min"][1], area["max"][1])
            height = self._terrain_height(terrain, x, z)
            slope = self._terrain_slope(terrain, x, z)

            # Terrain rules: no trees on steep slopes, nothing underwater,
            # nothing above the tree line
            if height < self.WATER_LEVEL:
                continue
            if kind == "tree" and height > self.TREE_LINE:
                continue
            if slope > self.MAX_SLOPE_DEG:
                continue

            # Density control: min spacing + per-cell cap
            if any(math.hypot(x - p["position"][0], z - p["position"][2])
                   < self.MIN_SPACING for p in placed):
                continue
            cell = (int(x // self.CELL_SIZE), int(z // self.CELL_SIZE))
            if cell_counts.get(cell, 0) >= self.MAX_PER_CELL:
                continue

            cell_counts[cell] = cell_counts.get(cell, 0) + 1
            placed.append({
                "id": len(placed),
                "kind": kind,
                "position": (x, height, z),
                "slope": slope,
            })

        self._distribution = {"seed": self.seed, "kind": kind,
                              "instances": placed}
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(self._distribution, fh)
        return self._distribution

    def _terrain_height(self, terrain, x, z):
        fn = terrain.get("height_fn")
        if callable(fn):
            return float(fn(x, z))
        grid = terrain.get("heightmap")
        if grid:
            res = len(grid)
            gx = min(max(int((x + 50.0) / 100.0 * (res - 1)), 0), res - 1)
            gz = min(max(int((z + 50.0) / 100.0 * (res - 1)), 0), res - 1)
            return float(grid[gz][gx])
        return 0.0

    def _terrain_slope(self, terrain, x, z, eps=0.5):
        dx = (self._terrain_height(terrain, x + eps, z)
              - self._terrain_height(terrain, x - eps, z)) / (2.0 * eps)
        dz = (self._terrain_height(terrain, x, z + eps)
              - self._terrain_height(terrain, x, z - eps)) / (2.0 * eps)
        return math.degrees(math.atan(math.hypot(dx, dz)))

    # ------------------------------------------------------------------
    # Animation: per-blade phase + wind direction + height gradient
    # ------------------------------------------------------------------
    def animate_vegetation(self, vegetation=None, dt=1.0 / 60.0, wind=None,
                           segments=4):
        vegetation = vegetation if vegetation is not None \
            else self.state.get("grass", [])
        wind = wind or self.wind
        self.time += dt

        wind_dir = _vec2_normalize(wind.get("direction", (1.0, 0.0)))
        strength = wind.get("strength", 1.0)
        speed = wind.get("speed", 2.0)

        animated = []
        for index, blade in enumerate(vegetation):
            phase = self._blade_phase(blade, index)
            # Sway along the wind direction, not against it
            sway = math.sin(self.time * speed + phase) * strength
            # Height gradient: root stays planted, tip swings the most
            segment_offsets = []
            for seg in range(segments + 1):
                t = seg / segments  # 0 at root, 1 at tip
                amplitude = t * t * sway
                segment_offsets.append((wind_dir[0] * amplitude,
                                        wind_dir[1] * amplitude))
            animated.append({
                "id": blade.get("id", index),
                "phase": phase,
                "segments": segment_offsets,
                "tip_offset": segment_offsets[-1],
            })

        result = {"time": self.time, "wind_direction": wind_dir,
                  "animated": animated}
        self.state["last_animation"] = result
        return result

    # ------------------------------------------------------------------
    # Culling: frustum + occlusion + distance, batched draw calls
    # ------------------------------------------------------------------
    def cull_vegetation(self, vegetation=None, camera=None, occluders=None):
        vegetation = vegetation if vegetation is not None \
            else self.state.get("grass", [])
        camera = camera or {"position": (0.0, 0.0, 0.0),
                            "forward": (0.0, 0.0, 1.0), "fov_deg": 90.0}
        occluders = occluders or []
        cam_pos = camera.get("position", (0.0, 0.0, 0.0))
        forward = camera.get("forward", (0.0, 0.0, 1.0))
        fov_cos = math.cos(math.radians(camera.get("fov_deg", 90.0)) / 2.0)

        visible = []
        culled = {"frustum": 0, "distance": 0, "occluded": 0}
        for index, inst in enumerate(vegetation):
            pos = inst.get("position", (0.0, 0.0, 0.0))
            to_inst = tuple(pos[i] - cam_pos[i] for i in range(3))
            dist = math.sqrt(sum(c * c for c in to_inst))

            # Distance culling: per-kind max view distance
            max_dist = (self.TREE_CULL_DISTANCE
                        if inst.get("kind") == "tree"
                        else self.GRASS_CULL_DISTANCE)
            if dist > max_dist:
                culled["distance"] += 1
                continue

            # Frustum culling
            if dist > 1e-6:
                dot = sum(to_inst[i] * forward[i] for i in range(3)) / dist
                if dot < fov_cos:
                    culled["frustum"] += 1
                    continue

            # Occlusion culling: an occluder between camera and instance
            if self._is_occluded(cam_pos, pos, dist, occluders):
                culled["occluded"] += 1
                continue

            visible.append(inst if "id" in inst else {**inst, "id": index})

        # Batching: one draw call per (kind, LOD bucket) instead of per blade
        batches = {}
        for inst in visible:
            pos = inst.get("position", (0.0, 0.0, 0.0))
            dist = math.dist(cam_pos, pos)
            lod = self._lod_for_distance(dist)
            lod_index = lod[0] if lod else len(self.LOD_LEVELS)
            key = (inst.get("kind", "grass"), lod_index)
            batches.setdefault(key, []).append(inst.get("id"))

        result = {
            "visible": visible,
            "culled": culled,
            "draw_calls": [
                {"kind": key[0], "lod": key[1], "count": len(ids),
                 "instances": ids}
                for key, ids in sorted(batches.items())
            ],
        }
        self.state["last_cull"] = result
        return result

    def _is_occluded(self, cam_pos, pos, dist, occluders):
        for occ in occluders:
            opos = occ.get("position", (0.0, 0.0, 0.0))
            radius = occ.get("radius", 1.0)
            to_occ = tuple(opos[i] - cam_pos[i] for i in range(3))
            occ_dist = math.sqrt(sum(c * c for c in to_occ))
            if occ_dist >= dist or occ_dist < 1e-6:
                continue
            # Angular separation between occluder and target directions
            dot = sum(to_occ[i] * (pos[i] - cam_pos[i]) for i in range(3))
            cos_angle = dot / (occ_dist * dist)
            cos_angle = max(-1.0, min(1.0, cos_angle))
            separation = occ_dist * math.sin(math.acos(cos_angle))
            if separation < radius:
                return True
        return False
