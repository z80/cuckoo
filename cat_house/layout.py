import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon
from matplotlib import cm
from shapely.geometry import Polygon
import random
import numpy as np
import copy

# Plywood dimensions
PLYWOOD_WIDTH = 96
PLYWOOD_HEIGHT = 48
ATTEMPTS = 20
GRID_RES = 1  # inch resolution

# Define pieces
base_pieces = [
    {'type': 'rect', 'width': 24, 'height': 16, 'label': 'Floor'},
    {'type': 'trap', 'top': 10, 'bottom': 12, 'height': 16, 'skew': 2, 'label': 'Left Side'},
    {'type': 'trap', 'top': 10, 'bottom': 12, 'height': 16, 'skew': 2, 'label': 'Right Side'},
    {'type': 'rect', 'width': 24, 'height': 10, 'label': 'Back Wall'},
    {'type': 'rect', 'width': 24, 'height': 12, 'label': 'Front Wall'},
    {'type': 'rect', 'width': 24, 'height': 22, 'label': 'Ceiling'},
    {'type': 'rect', 'width': 16, 'height': 6, 'label': 'Slide A'},
    {'type': 'rect', 'width': 16, 'height': 6, 'label': 'Slide B'},
]

def get_polygon(piece, x, y):
    if piece['type'] == 'rect':
        w, h = piece['width'], piece['height']
        if piece.get('rotated', False):
            w, h = h, w
        return Polygon([
            (x, y),
            (x + w, y),
            (x + w, y + h),
            (x, y + h)
        ])
    elif piece['type'] == 'trap':
        b, t, h, skew = piece['bottom'], piece['top'], piece['height'], piece['skew']
        if not piece.get('mirrored', False):
            points = [
                (0, 0),
                (b, 0),
                (b - skew, h),
                (0, h)
            ]
        else:
            points = [
                (skew, 0),
                (b + skew, 0),
                (b, h),
                (skew, h)
            ]
        if piece.get('rotated', False):
            points = [(py, px) for (px, py) in points]
        return Polygon([(x + px, y + py) for (px, py) in points])

def find_position(poly, placed):
    step = GRID_RES
    best_pos = None
    best_score = float('inf')
    for y in range(0, int(PLYWOOD_HEIGHT), step):
        for x in range(0, int(PLYWOOD_WIDTH), step):
            test = Polygon([(vx + x, vy + y) for (vx, vy) in poly.exterior.coords])
            if test.bounds[2] > PLYWOOD_WIDTH or test.bounds[3] > PLYWOOD_HEIGHT:
                continue
            if any(test.intersects(p) for p in placed):
                continue
            score = test.bounds[0] + test.bounds[1]
            if score < best_score:
                best_score = score
                best_pos = test
    return best_pos

def rasterize(placed):
    grid = np.zeros((int(PLYWOOD_HEIGHT / GRID_RES), int(PLYWOOD_WIDTH / GRID_RES)), dtype=np.uint8)
    for p in placed:
        minx, miny, maxx, maxy = p.bounds
        for x in range(int(minx), int(maxx)):
            for y in range(int(miny), int(maxy)):
                cell = Polygon([
                    (x, y), (x+1, y), (x+1, y+1), (x, y+1)
                ])
                if p.intersects(cell):
                    grid[y, x] = 1
    return grid

def largest_empty_rectangle(grid):
    rows, cols = grid.shape
    height = np.zeros((rows, cols), dtype=int)
    max_area = 0
    for y in range(rows):
        for x in range(cols):
            if grid[y, x] == 0:
                height[y, x] = height[y - 1, x] + 1 if y > 0 else 1
            else:
                height[y, x] = 0
        stack = []
        for x in range(cols + 1):
            h = height[y, x] if x < cols else 0
            while stack and h < height[y, stack[-1]]:
                last = stack.pop()
                w = x if not stack else x - stack[-1] - 1
                area = height[y, last] * w
                max_area = max(max_area, area)
            stack.append(x)
    return max_area

def mutate(config):
    new_config = copy.deepcopy(config)
    choice = random.choice(['swap', 'rotate', 'mirror'])
    if choice == 'swap':
        i, j = random.sample(range(len(new_config)), 2)
        new_config[i], new_config[j] = new_config[j], new_config[i]
    elif choice == 'rotate':
        candidates = [i for i, p in enumerate(new_config) if p['type'] == 'rect']
        if candidates:
            i = random.choice(candidates)
            new_config[i]['rotated'] = not new_config[i].get('rotated', False)
    elif choice == 'mirror':
        candidates = [i for i, p in enumerate(new_config) if p['type'] == 'trap']
        if candidates:
            i = random.choice(candidates)
            new_config[i]['mirrored'] = not new_config[i].get('mirrored', False)
    return new_config

def place(config):
    placed = []
    layout = []
    for piece in config:
        poly = get_polygon(piece, 0, 0)
        placed_poly = find_position(poly, placed)
        if placed_poly is None:
            return None, None
        placed.append(placed_poly)
        layout.append((piece, placed_poly))
    return layout, placed

def optimize():
    current_config = copy.deepcopy(base_pieces)
    for p in current_config:
        if p['type'] == 'rect':
            p['rotated'] = random.choice([False, True])
        elif p['type'] == 'trap':
            p['mirrored'] = p['label'].lower().startswith('right')
            p['rotated'] = False
    best_layout, best_placed = place(current_config)
    best_score = largest_empty_rectangle(rasterize(best_placed)) if best_placed else -1

    for attempt in range(ATTEMPTS):
        new_config = mutate(current_config)
        layout, placed = place(new_config)
        if placed:
            score = largest_empty_rectangle(rasterize(placed))
            if score > best_score:
                best_score = score
                best_layout = layout
                current_config = new_config
        print( f"Attempt {attempt} out of {ATTEMPTS}, score {score}, best score {best_score}" )
    return best_layout

def draw_layout(layout):
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.set_xlim(0, PLYWOOD_WIDTH)
    ax.set_ylim(0, PLYWOOD_HEIGHT)
    ax.set_aspect('equal')
    ax.set_title("Mutation-Based Optimized Layout")

    colors = cm.get_cmap('tab20', len(layout))
    for i, (piece, poly) in enumerate(layout):
        color = colors(i / len(layout))
        patch = MplPolygon(list(poly.exterior.coords), edgecolor='black', facecolor=color)
        ax.add_patch(patch)
        cx, cy = poly.centroid.x, poly.centroid.y
        ax.text(cx, cy, piece['label'], ha='center', va='center', fontsize=8)

    ax.plot([0, PLYWOOD_WIDTH, PLYWOOD_WIDTH, 0, 0],
            [0, 0, PLYWOOD_HEIGHT, PLYWOOD_HEIGHT, 0], 'k--')
    plt.tight_layout()
    plt.show()

# Run optimizer and visualize
best = optimize()
draw_layout(best)

