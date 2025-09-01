import pcbnew

# Load the current board
board = pcbnew.GetBoard()

# Iterate through all footprints
for footprint in board.GetFootprints():
    value_text = footprint.Value()
    if value_text.GetLayerName() == "F.Fab":
        # Move value to F.SilkS layer
        value_text.SetLayer(pcbnew.F_SilkS)
